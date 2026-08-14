// authConfigGuard — the UNCONDITIONAL Entra fail-fast (E36/T19, research item 9).
//
// Pure, framework-free, `.test.ts`-pinned — the `opsStatus.ts` / `cedarPosture.ts` idiom, and
// here it is a hard requirement rather than a preference: `vitest.config.ts` collects
// `src/**/*.test.ts` under the NODE environment (no jsdom, no happy-dom), and `msalConfig.ts`
// constructs a `PublicClientApplication` and reads `window.location.origin` at module scope.
// So `msalConfig.ts` is not importable in the test lane at all, and the only way this decision
// is testable is for it to live here.
//
// ---------------------------------------------------------------------------
// WHY THIS MODULE EXISTS
//
// The old guard (`msalConfig.ts:8-18`) threw only inside
// `if (import.meta.env.VITE_AUTH_PROVIDER === 'entra')`. Three facts make that gate a hole
// rather than a condition:
//
//   1. `VITE_AUTH_PROVIDER` is typed OPTIONAL (`vite-env.d.ts:5`, `?: 'entra'`) and Entra is
//      the SOLE provider, so the variable selects nothing. It only ever gates.
//   2. Exactly one build path writes it. `deploy-full.sh` does (`:329`) — and since E34/T11
//      that script also refuses to deploy with missing or placeholder Entra values
//      (`:139-162`), which closes the hole on the MAIN deploy path. Every OTHER path leaves it
//      to whatever is on disk: `deploy-frontend.sh` "does NOT generate any env files" and runs
//      `npm run build` as-is, and a bare `npm run build` or a CI job does the same.
//   3. With the key absent the throw was skipped and the module FELL BACK — `clientId: ''` and
//      the `/common` multi-tenant authority. That is the harmful half: `/common` is a WORKING
//      authority pointing at the wrong directory, so sign-in proceeds and every API call 401s
//      on issuer, with no diagnostic anywhere in the bundle.
//
// So the guard is unconditional now, and this module does not read `VITE_AUTH_PROVIDER` AT
// ALL — the property is structural, not a branch that happens to be true. Both fallbacks are
// deleted with it: they existed ONLY to keep the module importable when the throw was skipped,
// and with no skip they have no purpose except to hide the failure they were built for.
//
// ---------------------------------------------------------------------------
// WHY "UNUSABLE" IS THE DEPLOY SCRIPT'S PREDICATE AND NOT A NEW ONE
//
// `isUnusableAuthValue` mirrors `frontend_var_unusable()` in
// `infrastructure/scripts/deploy-full.sh:86-91` clause for clause — empty, angle-bracketed
// stub, all-zero GUID. Two spellings of one rule is the drift this epic exists to remove: a
// value the deploy preflight refuses at the door must be a value the bundle refuses at module
// load, or the two gates disagree about what "configured" means.
//
// It matters that a PLACEHOLDER counts. All three `.env*.example` files ship
// `00000000-0000-0000-0000-00000000000{1,2}`, so `cp .env.example .env` and forget is the
// common way sign-in dies — and the value LOOKS filled in, which is precisely why a
// missing-value-only check is not enough.
//
// TWO NON-GOALS, recorded so they are decisions rather than omissions:
//
//   • NO GUID SHAPE VALIDATION. The deploy twin does none either, and a syntactically valid
//     GUID that names the wrong directory is not detectable here — it fails loudly at Entra.
//     Adding a shape check here would make the two gates disagree in the other direction.
//   • A TENANT ID OF `common` (or `organizations` / `consumers`) is NOT refused, so the
//     `/common` authority is technically still reachable — by someone typing it, which is a
//     deliberate act rather than the silent absence this module closes. Nothing in this repo
//     produces it: no example file carries it and no script writes it, and inventing a
//     rejection for a state no writer produces is the error `opsStatus.ts` documents when it
//     deleted union members nothing emitted. The backend's issuer check refuses tokens from a
//     foreign directory regardless (`401 Invalid issuer (expected …)`), and since T19 that
//     401 is bounded — see `api/authRetry.ts`.
//
// ---------------------------------------------------------------------------
// WHY FAILING HARD IS RIGHT FOR `npm run dev` TOO
//
// An unconditional throw means a dev server with no env file dies at module load instead of
// rendering. That is the intended behaviour, not collateral: the current alternative is a
// blank page after a sign-in that cannot complete, so the throw replaces a mystery with a
// message. And a CONFIGURED tree is untouched — Vite loads `frontend/.env` as the base file in
// dev and production alike, so a developer who followed `.env.local.example` (or who has the
// repo's own gitignored `.env`) never sees this.
// ---------------------------------------------------------------------------

/**
 * The two `import.meta.env` keys this guard reads — and the complete set.
 *
 * Exported so the test asserts against the same literals the code uses: a typo in a key name
 * would otherwise read as "absent" and throw on a correctly configured tree, which is the one
 * failure mode of a fail-fast guard that is worse than the hole it closes.
 *
 * `VITE_ENTRA_SPA_REDIRECT_URI` and `VITE_ENTRA_SPA_SCOPE` are deliberately NOT here. Both
 * have real, working defaults in `msalConfig.ts` and neither can silently authenticate against
 * the wrong directory, which is the specific harm this module exists to prevent.
 */
export const AUTH_ENV_KEYS = {
  tenantId: 'VITE_ENTRA_TENANT_ID',
  clientId: 'VITE_ENTRA_SPA_CLIENT_ID',
} as const;

/** Entra's authority host. One place, so the authority cannot be assembled twice. */
const AUTHORITY_HOST = 'https://login.microsoftonline.com';

/**
 * `true` when a value cannot be used — the twin of `frontend_var_unusable()` in
 * `infrastructure/scripts/deploy-full.sh:86-91`, whose own comment states the rule this
 * follows: "an unfilled example placeholder is exactly as broken as a missing value … and an
 * all-zero GUID both count as absent. No real tenant or client id is all zeros."
 *
 * Trimmed first, because a `.env` file is hand-edited and a trailing space on an otherwise
 * good GUID must not be treated as a configured value — it would be interpolated into the
 * authority URL.
 */
export function isUnusableAuthValue(raw: string | undefined): boolean {
  const value = (raw ?? '').trim();
  if (value === '') return true;
  // An angle bracket anywhere means an unfilled `<api-id>` / `<cloudfront-domain>` stub.
  if (value.includes('<') || value.includes('>')) return true;
  // The example placeholders, and any other GUID whose first four groups are zeros.
  return value.startsWith('00000000-0000-0000-0000-');
}

/**
 * The MSAL `auth` values, or a throw naming exactly which keys to fix.
 *
 * Takes the env RECORD rather than reading `import.meta.env` itself — that is what keeps it
 * testable in a lane where `import.meta.env` is not the browser's, and what makes "regardless
 * of `VITE_AUTH_PROVIDER`" verifiable: the function is handed the whole env and reads two keys
 * of it.
 *
 * THROWS AT MODULE LOAD by construction (its only caller is `msalConfig.ts`'s top level), and
 * that timing is the point: the alternative is a `clientId` MSAL accepts and a directory that
 * is not ours, discovered later as a 401 with no diagnostic.
 */
export function resolveAuthConfig(
  raw: Record<string, string | undefined>,
): { clientId: string; authority: string } {
  const tenantId = (raw[AUTH_ENV_KEYS.tenantId] ?? '').trim();
  const clientId = (raw[AUTH_ENV_KEYS.clientId] ?? '').trim();

  // Collected rather than short-circuited: an operator filling in a fresh `.env` usually has
  // both keys wrong, and reporting one, failing, then reporting the other is two round trips
  // through a build. `deploy-full.sh` reports its missing set the same way.
  const unusable = [
    isUnusableAuthValue(tenantId) ? AUTH_ENV_KEYS.tenantId : null,
    isUnusableAuthValue(clientId) ? AUTH_ENV_KEYS.clientId : null,
  ].filter((key) => key !== null);

  if (unusable.length > 0) {
    // The message must not name `VITE_AUTH_PROVIDER` (the old one led with it): setting that
    // variable now fixes nothing, and pointing at it would send the reader to the wrong file.
    throw new Error(
      `Entra sign-in is not configured: ${unusable.join(', ')} ` +
        `${unusable.length === 1 ? 'is' : 'are'} missing or still a placeholder. ` +
        'Set it in platform/control_plane/frontend/.env (see .env.local.example / ' +
        '.env.production.example) and rebuild. Where each value comes from: docs/entra-setup.md.',
    );
  }

  return { clientId, authority: `${AUTHORITY_HOST}/${tenantId}` };
}
