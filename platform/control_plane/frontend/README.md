# Control-plane frontend

The single-page app operators use to govern their agent estate: the agent and MCP server registries,
the access tabs that grant users and agents access, the Cedar tool policies, the governance graph, the
marketplace, and the observability and cost views. It is a React + TypeScript SPA built with Vite,
signed in through Microsoft Entra ID with MSAL, and served from S3 behind CloudFront.

It is a pure client of the [control-plane API](../backend/README.md) — it holds no business logic of
its own and stores no credential beyond MSAL's own token cache. See the root
[README](../../../README.md) for what the platform does.

---

## Stack

| | |
|---|---|
| **Framework** | React 19, TypeScript 5.9, Vite 8 |
| **Styling** | Tailwind CSS 4, configured CSS-first (`@import "tailwindcss"` in `src/index.css`) — there is no `tailwind.config.js` |
| **Auth** | `@azure/msal-browser` + `@azure/msal-react` v5 |
| **Routing** | React Router 7 |
| **Graph** | `@xyflow/react` + dagre |
| **Charts** | Recharts |
| **HTTP** | axios |

```
src/
├── main.tsx        # entry — resolves the UI flavor, then createRoot
├── App.tsx         # routes and the app shell
├── auth/           # MSAL provider + the shared auth shape
├── api/            # typed API client
├── components/
│   ├── governance/ # registries, access tabs, graph, marketplace, observability
│   ├── govern/     # the governance surfaces that are design-only (see below)
│   ├── guardrails/
│   ├── operations/ # projects, builds, connections
│   └── shared/     # cross-page primitives, including the honesty copy
├── contexts/
└── ui/             # UI-flavor preference
```

### The UI is Classic, and that is deliberate

`src/ui/uiPreference.ts` reads a persisted UI flavor with two values, `classic` and `cloudscape`, and
**`classic` is what ships**. The Cloudscape arm is parked: a migration was attempted, evaluated, and
reverted, and the code is preserved at the `e31c-complete` / `e31d-parked` tags rather than in this
tree. `main.tsx` keeps a one-line seam so a future change only has to add the render arm.

Two things follow, and both are enforced expectations rather than preferences:

- **There is no `@cloudscape-design` dependency in `package.json`, and none should be added** while the
  Classic UI ships. An import of a package that is not installed fails the build; an import of one
  that someone installs "just for this component" reopens a migration that was deliberately closed.
- The lint gate below judges the **Classic UI** tree and has no baseline: every finding it reports is
  one on this tree, none inherited.

---

## Configuration

The SPA reads exactly **seven** `VITE_*` variables. They are declared, once, in two places that are
kept in step — [`.env.example`](.env.example), which explains where each value comes from, and
[`src/vite-env.d.ts`](src/vite-env.d.ts), which types them. **Read those two files; this README
deliberately does not restate the list**, because a third copy is a third thing to drift.

```bash
cp .env.example .env    # then fill it in
npm ci
npm run dev             # Vite on :5173
```

Two of the seven — the API URL and the redirect URI — are not knowable until the stack has been
deployed once, because they are the API Gateway endpoint and the CloudFront domain. The root
README's bootstrapping section walks that loop, and
[`docs/entra-setup.md`](../../../docs/entra-setup.md) is where the Entra values come from.

**The `devauth` arm reads none of them.** The variables above are the Entra arm's. `npm run dev:devauth`
runs Vite in the `devauth` mode, the dev-login arm `agp up` uses: the SPA is signed in as `dev@example.com`
with no Microsoft session, against a backend started with `USE_DEV_AUTH=true` on loopback, and reads only
`VITE_API_URL` and `VITE_BUILD_SHA`. The mode swaps the auth modules under `src/auth/` that have a twin under
`src/auth/devauth/` at build time; every other build fails in `vite.config.ts` at load time when a module
under `src/auth/devauth/` enters its graph, and again when an emitted chunk carries the whole word `devauth`;
`deploy-frontend.sh` refuses such a bundle, so no other build can carry it.

**Which env file the production build reads.** `.env` is the file to hand-edit and the one the deploy
script reads. Vite ranks env files for a production build in ascending priority `.env` → `.env.local`
→ `.env.production` → `.env.production.local`, so `.env.production` — which the deploy script
regenerates from `.env` — is what the bundle is actually built from. Editing `.env` and then running a
frontend-only redeploy therefore ships the *previous* values, silently. The root README says this
too, in more detail; it is the single most expensive mistake available here.

---

## Gates

All five run from this directory. A change should leave every one of them no worse than it found it.

```bash
npx tsc -b            # types
npx vitest run        # every src/**/*.test.ts, node-only
npm run build         # tsc -b && vite build
npm run lint          # eslint . — fails on any error-level finding
npm audit --omit=dev  # expects 0 vulnerabilities
```

`npm run generate:api` rewrites `src/api/openapi.ts` from the backend's reviewed snapshot
(`../backend/tests/fixtures/openapi.json`), and `src/api/openapi.test.ts` fails until the committed file is
that command's output, so a backend change reaches the frontend as a regenerated file, never a hand edit.

### Tests are node-environment only, by design

`vitest.config.ts` sets `include: ['src/**/*.test.ts']` — note the extension. There are **no `.tsx`
tests, no DOM environment, and no render harness**, and that is a decision rather than a gap: what is
covered here is the logic that can be extracted from a component (mappers, guards, reducers, URL and
policy builders, preference resolution), and component wiring is covered by `npm run build` plus
actually deploying and clicking. A `.test.tsx` file would not be picked up at all, so adding one looks
like passing tests and is not.

The practical consequence: **to make behaviour testable here, move it out of the `.tsx` file into a
`.ts` module** and test that. The honesty copy below is the clearest example of the pattern.

### The honesty copy is pinned and frozen

Ten pages of the app are designed but not yet functional, and each says so. The wording is not
retyped per page — the four strings live in
[`src/components/shared/comingSoonCopy.ts`](src/components/shared/comingSoonCopy.ts) (a pure
constants module with no React import, precisely so it can be covered by the node-env suite) and
`comingSoonCopy.test.ts` **asserts each one by exact equality**.

That test failing is not a wording preference to be updated — it means a surface has started
disagreeing with every other surface about how honest the platform is being. Change the constant and
every page changes with it, which is the point.

The components that render it, in
[`src/components/shared/comingSoon.tsx`](src/components/shared/comingSoon.tsx), carry four visual
decisions, and `comingSoon.test.ts` holds each against the source. The banner and the badges are amber,
never rose: a not-yet-live feature is a caveat, not a failure, and rose is reserved for real breakage.
The banner is non-dismissible and carries `role="status"`, not `role="alert"`, so it informs without
interrupting. `SoonTag`, the chip beside a nav row, is a rounded rectangle (`rounded-[5px]`), never the
`rounded-full` pill of `SampleBadge` and `ComingSoonBadge`, so "this row isn't built" and "this widget
is illustrative" differ by shape at a glance; and it is slate, not amber, because a nav row is scanned
in a dense list.

### The lint has no baseline

`npm run lint` is a gate with no baseline: ESLint reports every finding, and the gate exits 0 and fails
on any error-level one. The two rules the plugin ships at `warn` and this config does not raise
(`react-hooks/incompatible-library`, `react-hooks/unsupported-syntax`) stay warnings and are zero on the
tree, while `exhaustive-deps`, the third, is raised to `error`. A change that adds a finding fails the
gate: fix the finding.

A suppression that survives is one a reader can audit: a new inline `eslint-disable` needs
`-- <reason>; expires <YYYY-MM-DD>; added <origin>` (`agp/suppression-tail`, with
`@eslint-community/eslint-comments/require-description`), a directive that no longer suppresses
anything fails (`reportUnusedDisableDirectives`), and `@ts-ignore` is refused outright
(`ban-ts-comment`); a suppressed rule without a reason is a lie that lints clean.

### Conventions the lint holds

Each of these is held by a tool, not by review, so a reader can tell them from preferences.

- **A fetch goes through `useLoad`** ([`src/api/useLoad.ts`](src/api/useLoad.ts)), and loading is
  derived, never set: the hook keeps the settled record of the last request and answers `loading` while
  the current inputs differ from it, so no effect writes `setLoading(true)` and
  `react-hooks/set-state-in-effect` stays on. Callers list their dependencies — `exhaustive-deps` checks
  `useLoad` call sites through `additionalHooks`, as an error. The migrated pages read through it; the
  decision, and the effects that stay outside it by decision, are in
  [`docs/adr/frontend-loading-state.md`](../../../docs/adr/frontend-loading-state.md); a fetch effect
  the migration has not reached is unfinished work, not an exception.
- **A `.tsx` exports components only.** Helpers, constants and types a test needs live in a `.ts`
  sibling; `react-refresh/only-export-components` refuses the other shape, and the node-only suite
  (above) is the reason the sibling exists.
- **No comment the lint refuses.** `scripts/lint/comments.sh` rejects an added comment that carries an
  epic or task code, a review round, a banner or a band-aid note, in pre-commit and in CI alike. What a
  comment used to hold has four homes: an invariant becomes a test name or a type, a UI trap a bullet
  under `## Gotchas` in the feature map (`.claude/skills/verify-agp/features/`), a cross-cutting decision
  an ADR under [`docs/adr/`](../../../docs/adr/), and history the commit message. Narration is deleted.
- **A comment-density ceiling.** [`src/commentDensity.test.ts`](src/commentDensity.test.ts) measures
  every non-test `.ts`/`.tsx` under `src` and fails a file with more comment-only lines than one in ten,
  once it carries enough of them to be judged; the failure names the file and the four homes. A module
  with ten comment-only lines or fewer is never judged.
- **Raw-source tests assert on code, not prose.** A test that reads a source file with `?raw` asserts on
  `stripComments(src)` ([`src/rawSource.ts`](src/rawSource.ts)), so no guard is satisfied by a comment
  that happens to contain the right words.
- **Suppressions carry a reason, an expiry and their origin, and are used.** The rules the no-baseline
  section names: an `eslint-disable` without `-- <reason>; expires <YYYY-MM-DD>; added <origin>`, an
  unused directive and `@ts-ignore` each fail the gate.

---

## Contributing

Read [`CONTRIBUTING.md`](../../../CONTRIBUTING.md) for the gates a change must pass and the commit
convention.
