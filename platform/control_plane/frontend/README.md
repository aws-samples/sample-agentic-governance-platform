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
├── types/
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
- The lint baseline below is a **Classic-UI baseline**. It was measured on this tree, not inherited.

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
npx vitest run        # 44 files, 1290 tests
npm run build         # tsc -b && vite build
npm run lint          # eslint . — read the note below before reacting to the number
npm audit --omit=dev  # expects 0 vulnerabilities
```

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

### The lint baseline

`npm run lint` reports **63 problems (59 errors, 4 warnings) and exits 1.** This is a known, accepted
baseline, not new debt. Treat the number as the bar: a change should not raise it.

| Count | Rule |
|---|---|
| 26 | `react-hooks/set-state-in-effect` |
| 22 | `react-refresh/only-export-components` |
| 8 | `@typescript-eslint/no-explicit-any` |
| 4 | unused `eslint-disable` directives (the 4 warnings) |
| 3 | one each: `no-empty`, `react-hooks/preserve-manual-memoization`, `@typescript-eslint/no-unused-vars` |

The two large groups are the shape of the Classic UI itself: long-lived pages that set state in an
effect after a fetch, and modules that export a helper next to their component. Both are worth fixing
and neither is a defect, so they were left visible instead of being suppressed with inline disables —
a suppressed rule is a lie that lints clean.

---

## Contributing

Read [`CONTRIBUTING.md`](../../../CONTRIBUTING.md) for the gates a change must pass and the commit
convention.
