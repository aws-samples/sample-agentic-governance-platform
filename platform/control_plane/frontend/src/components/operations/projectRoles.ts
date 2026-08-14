// Pure per-project-role helpers (E27/T11). Framework-free so vitest picks them up
// — only `src/**/*.test.ts` is collected, so EVERY role decision lives here and the
// `.tsx` files (ProjectAccessTab, ProjectDetail) are wiring only. The `tenantUi.ts`
// + `tenantUi.test.ts` pair is the precedent.
//
// A project role grants ONE Entra principal (a user OR a group, by object id)
// authority over ONE project: viewer < maintainer < owner. It is checked IN ADDITION
// to tenant visibility server-side, never instead of it — so nothing here is a
// security boundary. The backend's `may()` gate is the real authority; these helpers
// decide which affordances are worth rendering (and per the epic's FE constraint a
// gated affordance is CONDITIONALLY RENDERED, never `disabled` — `disabled` is
// reserved for in-flight requests).
//
// The caller's own role comes from the SERVER (`ProjectDetail.effective_role`), not
// from the roster — see `effectiveRole` below. The roster cannot answer it, because
// a role may be granted to an Entra GROUP and the browser cannot evaluate group
// membership.

import type { ProjectRoleRecord } from '../../api/client';
import type { OPS_BADGE } from './opsUi';
// The single exhaustive status tables (E28/T10). The two `cicd_status` helpers below now
// DELEGATE here instead of carrying their own tables — see the comment above them for why
// a shared table plus a `tsc`-enforced closed union replaced a "do not re-fork this" note.
import { CICD_BADGE_KEY, CICD_LABEL, toCicdStatus } from './opsStatus';

// The shape `effectiveRole` reads — structural, so it accepts a whole `ProjectDetail`
// without this module importing the page's data type.
export interface EffectiveRoleSource {
  effective_role?: string | null;
}

export type ProjectRoleName = 'viewer' | 'maintainer' | 'owner';

export const PROJECT_ROLE_OPTIONS: { value: ProjectRoleName; label: string; hint: string }[] = [
  { value: 'viewer',     label: 'Viewer',     hint: 'Read the project and its repositories' },
  { value: 'maintainer', label: 'Maintainer', hint: 'Add and retry repositories; change agents' },
  { value: 'owner',      label: 'Owner',      hint: 'Full control, including promote to prod and delete' },
];

const RANK: Record<ProjectRoleName, number> = { viewer: 0, maintainer: 1, owner: 2 };

/** Does `held` meet `required`? `null` = no grant. Admins are handled by roleLevel, not here. */
export function meetsRole(held: ProjectRoleName | null, required: ProjectRoleName): boolean {
  return held !== null && RANK[held] >= RANK[required];
}

/** Owner-or-admin may manage roles. roleLevel 2 = Platform.Admin. */
export function canManageRoles(held: ProjectRoleName | null, roleLevel: number): boolean {
  return roleLevel >= 2 || meetsRole(held, 'owner');
}

// ---------------------------------------------------------------------------
// canPromote — Promote requires Owner (or platform admin) AND a PENDING PROD CANDIDATE.
//
// E27A moved the precondition. It used to be `last_dev_image_tag` — "something reached dev,
// so there is an image to ship" — which made promotion an image-picker with one entry. It is
// now `prod_candidate_status`, because promotion IS THE APPROVAL OF A MERGE TO `main`: the
// candidate route registers one candidate per merge (from a validated GitHub OIDC token) and
// a successful promote clears it. So the question the button answers changed from "is there
// an image?" to "is there a merge waiting for your approval?", and the backend agrees —
// `promote_repo` reads `prod_candidate_image_tag` and answers 409 "no prod candidate to
// promote" when nothing is pending.
//
// Compared to the LITERAL 'pending', not truthiness. Unlike `cicd_status` (a free-form string
// with five writers, one of them a shell helper) this field has exactly one writer and one
// value, so an unrecognised string is not a candidate — and a truthiness check would happily
// treat a leftover dev image tag, if ever passed here by mistake, as approvable.
// ---------------------------------------------------------------------------
export function canPromote(
  held: ProjectRoleName | null,
  roleLevel: number,
  prodCandidateStatus: string | null,
): boolean {
  return prodCandidateStatus === 'pending' && (roleLevel >= 2 || meetsRole(held, 'owner'));
}

// ---------------------------------------------------------------------------
// prodCandidateView — the pending candidate, shaped for the row that DESCRIBES WHAT IS
// BEING APPROVED (E27A). The row reads:
//
//     main @ 3f9a1c2 · pushed by @jorge · awaiting your approval
//
// That sentence is the feature. Promotion is no longer "ship the latest image", it is an
// owner taking responsibility for ONE named commit by ONE named author, so the affordance
// without the description would be a button with no subject.
//
// THE VERB IS "pushed by" (E28/T13, A3), and this comment is the fifth site that had to change:
// it documented the old merge-flavoured wording as the intended design ON THE FUNCTION THAT
// PRODUCES THE ACTOR, which would have instructed the next author to reintroduce it. Nothing
// records that a merge happened — the candidate is registered on ANY push to `main` and the actor
// comes only from an OIDC token — so the wording lives in `repoRowModel.CANDIDATE_ACTOR_VERB`
// with a test and its full trace. Only comments changed here; the shaping is untouched.
//
// Two deliberate shaping decisions:
//
//   • The sha is truncated to SEVEN, matching both git's own short form and the `[:7]` the
//     image tag embeds — so `3f9a1c2` and `agent-7-3f9a1c2` visibly line up, and an owner
//     can see that the image they are shipping was built from the commit named beside it.
//   • The actor is prefixed `@` because it is a GITHUB LOGIN, not an Entra principal. The
//     row's other identity line (`last_promoted_by`) is a raw Entra oid, and the two are
//     deliberately NOT joined (design §6): `@jorge` reads as "the person on the provider who
//     pushed this", and the `@` is what marks it as the provider's currency rather than
//     implying AGP resolved it to a platform user.
//
// Returns null unless the status is literally 'pending' — the same comparison `canPromote`
// makes, so the description and the button can never disagree (pinned in the tests). The
// three display fields narrow to null individually: one route writes all five together, so a
// missing piece means a partial row, and a bare `main @ ` or a lone `@` would be worse than
// omitting it on the one line whose job is to name exactly what ships.
// ---------------------------------------------------------------------------
export interface ProdCandidateSource {
  prod_candidate_status?: string | null;
  prod_candidate_sha?: string | null;
  prod_candidate_actor?: string | null;
  prod_candidate_image_tag?: string | null;
  prod_candidate_at?: string | null;
}

export interface ProdCandidateView {
  /** The merge commit, git-short (7). Null when the wire field is absent/blank. */
  shortSha: string | null;
  /** The GitHub login, `@`-prefixed exactly once. Null when absent/blank. */
  actor: string | null;
  /** The image the merge built — displayed, never sent. */
  imageTag: string | null;
  /** ISO-8601 UTC, as delivered; the row shows the date only. */
  at: string | null;
}

export function prodCandidateView(
  repo: ProdCandidateSource | null | undefined,
): ProdCandidateView | null {
  if (repo?.prod_candidate_status !== 'pending') return null;
  const text = (v: string | null | undefined): string | null => {
    const s = (v ?? '').trim();
    return s.length > 0 ? s : null;
  };
  const sha = text(repo.prod_candidate_sha);
  const actor = text(repo.prod_candidate_actor);
  return {
    shortSha: sha === null ? null : sha.slice(0, 7),
    actor: actor === null ? null : actor.startsWith('@') ? actor : `@${actor}`,
    imageTag: text(repo.prod_candidate_image_tag),
    at: text(repo.prod_candidate_at),
  };
}

// ---------------------------------------------------------------------------
// promoteBlockedReason — WHY is Promote not offered on this row?
//
// `canPromote` folds two very different refusals into one `false`, and the difference is
// the one the repo already treats as load-bearing (see ProjectDetail's Delete-project
// comment): "not yet" is a precondition the operator can act on and deserves a sentence,
// whereas "not you" is a role refusal that must show NOTHING — a hint reading "you need
// Owner" on every row is both noise and an invitation to go asking for privilege.
//
// So: 'ok' → render the button; 'no-candidate' → render the one-line explanation;
// 'not-owner' → render nothing at all. Kept in step with `canPromote` by construction —
// 'ok' is exactly `canPromote(...) === true`, pinned in the tests.
//
// 'no-candidate' is E27A's rename of 'no-dev-image', and the copy changed with it: the
// remedy is no longer "push to the dev branch" (a dev build is now routine and says nothing
// about prod) but "merge to main", which is the act that creates a candidate.
// ---------------------------------------------------------------------------
export type PromoteBlockedReason = 'ok' | 'no-candidate' | 'not-owner';

export function promoteBlockedReason(
  held: ProjectRoleName | null,
  roleLevel: number,
  prodCandidateStatus: string | null | undefined,
): PromoteBlockedReason {
  if (!(roleLevel >= 2 || meetsRole(held, 'owner'))) return 'not-owner';
  return prodCandidateStatus === 'pending' ? 'ok' : 'no-candidate';
}

// ---------------------------------------------------------------------------
// promotionStatusLabel / cicdBadgeKey — a repo's `cicd_status` → the label and the tint
// its CI/CD pill shows.
//
// E28/T10 MOVED THE TABLES, not the call sites. Both helpers keep their names and their
// signatures — every existing caller is untouched — but each now narrows the bare wire
// string through `toCicdStatus` and reads the single exhaustive table in `opsStatus.ts`.
//
// WHY. These two used to hold the tables themselves, and that was already the second
// attempt at this problem. The first was two module-private copies of the tint table (one
// per page); when E27 added `promoting` / `deployed` only one copy was extended, both new
// statuses fell through to the amber `pending` on the other page, and A REPO LIVE IN
// PRODUCTION RENDERED IN THE SAME AMBER AS ONE STILL PROVISIONING. The fix consolidated
// them here and left a comment asking the next author not to re-fork them.
//
// That comment is an honour system, and it fails the moment a second surface legitimately
// needs a different visual treatment — which is what E28 builds. `opsStatus.ts` replaces it
// with a mechanism: the statuses are a CLOSED UNION and the tables are
// `Record<CicdStatus, X>` with no `default`, so a new status without a table entry is a
// COMPILE ERROR. `tsc` now enforces what the comment merely requested.
//
// Two behaviour changes came with the move, both deliberate and both pinned in the tests:
//   • An UNRECOGNIZED value now reads "No status reported" in a neutral slate, instead of
//     "Provisioning" in amber. The old fall-through stated a state nobody had established —
//     the same class of error as the bug above.
//   • `ready` (materialize's `finalize`) now wears the amber `pending` instead of emerald.
//     It means the repo is SCAFFOLDED and has never built or deployed, so an emerald
//     "Ready" pill was a terminal-success tint over a repo that had shipped nothing.
//
// Kept as named wrappers rather than deleted in favour of direct `opsStatus` calls: they
// are the vocabulary the promote surface already speaks, and re-pointing ~4 call sites in
// files other Wave-2 tasks own would be scope this task does not need. They are now one
// line each, so there is nothing left in them to drift.
// ---------------------------------------------------------------------------
export function promotionStatusLabel(cicdStatus: string | null | undefined): string {
  return CICD_LABEL[toCicdStatus(cicdStatus)];
}

export function cicdBadgeKey(status: string | null | undefined): keyof typeof OPS_BADGE {
  return CICD_BADGE_KEY[toCicdStatus(status)];
}

// ---------------------------------------------------------------------------
// isPromotionInFlight — is a PROD PROMOTION running for this repo right now? The one
// status that both suppresses a second Promote (the route refuses it with a 409) and
// puts the row into its in-flight treatment.
//
// Deliberately NARROWER than `opsStatus.isCicdInFlight`, which is true for any in-flight
// delivery state (`provisioning` / `building` / `promoting`). This one is true for
// `promoting` ALONE, because its callers ask a specific question — "is a promotion in
// flight?" — and two of them are load-bearing:
//   • `keepPromotionOverride` compares this value on the live and parent records to decide
//     whether the locally-polled row still bridges the promotion window. Widening it would
//     make an ordinary dev build look like a promotion boundary.
//   • The Promote affordance is suppressed on it; the route 409s a second promotion, but
//     not a promotion during a dev build.
// Kept as two functions rather than one because they answer different questions; the
// difference is pinned in both test files.
// ---------------------------------------------------------------------------
export function isPromotionInFlight(cicdStatus: string | null | undefined): boolean {
  return toCicdStatus(cicdStatus) === 'promoting';
}

// ---------------------------------------------------------------------------
// keepPromotionOverride — may the repos table keep its LOCALLY-POLLED record for a row,
// or must it fall back to the one the parent's project read reports?
//
// The table renders `liveRepos[id] ?? fromParent`, where `liveRepos` holds the 202 body and
// the `getRepoStatus` ticks after it. That override exists for exactly ONE reason: to bridge
// the window in which a promotion is running and the parent's read does not know it yet. It
// must therefore not survive that window — and the first cut of the rule ("keep it whenever
// it DIFFERS from the parent") did the opposite. Differing is precisely the case where the
// PARENT may be the newer read, and once a promotion is terminal the poller tears itself
// down, so nothing ever refreshed or dropped the override again: promote → `deployed`
// cached → a later dev build writes `failed` out-of-band → the parent correctly reports
// `failed` → 'failed' !== 'deployed' → the override was KEPT and the row showed emerald
// "Deployed" over a red build until a full reload. Wrong-and-reassuring, on the one row
// whose question is "what is in production right now?".
//
// So the rule is the bridge itself, in one sentence: keep the override only while it
// DISAGREES WITH THE PARENT ABOUT WHETHER A PROMOTION IS IN FLIGHT.
//   • live promoting, parent not  → keep. The bridge, entering: the 202/poll knows about a
//     promotion the parent's older read predates.
//   • live terminal, parent promoting → keep. The bridge, leaving: `promoting` can only
//     precede `deployed`/`failed`, so a terminal override IS the strictly later read.
//   • both agree (whatever the statuses) → DROP. The parent has caught up, so it is at
//     least as new; anything the override would add beyond that is unverifiable.
//   • the row is gone from the parent → DROP. A deleted repo must not be resurrected.
//
// Note it is deliberately NOT an `updated_at` comparison (the reviewer's other suggestion):
// the buildspec writes `cicd_status` through a targeted `update-item` that never touches
// `updated_at`, so that timestamp does not move when the status does — comparing it would
// be a freshness proxy that is simply wrong for the writer this exists to observe.
// ---------------------------------------------------------------------------
export function keepPromotionOverride(liveStatus: string, parentStatus: string | undefined): boolean {
  if (parentStatus === undefined) return false;
  return isPromotionInFlight(liveStatus) !== isPromotionInFlight(parentStatus);
}

// ---------------------------------------------------------------------------
// canDestroy — may this caller be OFFERED the OWNER-gated DESTRUCTIVE verbs?
// Two surfaces: "Delete project" (routes/projects.py delete_project) and the
// per-row "Delete repository", which is the IRREVERSIBLE E23 five-item cascade
// (runtime + TF state, ECR images, GitHub repo, Entra identity, record).
//
// Same threshold as canManageRoles — both routes gate on OWNER, and the design-§3
// ungoverned-project fallback deliberately stops at MAINTAINER, so it never reaches
// either verb. Delegated rather than re-derived so "owner-or-admin" has ONE body;
// named separately because these are the higher-consequence verbs and a future
// divergence (e.g. a break-glass admin-only delete) should be one edit here, not a
// search for every `canManageRoles` call site.
// ---------------------------------------------------------------------------
export function canDestroy(held: ProjectRoleName | null, roleLevel: number): boolean {
  return canManageRoles(held, roleLevel);
}

// ---------------------------------------------------------------------------
// mayMaintainProject — may this caller be OFFERED the MAINTAINER-gated repository
// verbs? Two surfaces: "New repository from template" (POST /{id}/repos) and
// "Retry from failed step" (POST /{id}/repos/{repo}/retry).
//
// NOT just `meetsRole(held, 'maintainer')`. These two routes gate through
// `_require_project_role_or_ungoverned`, so unlike the OWNER verbs they DO get the
// design-§3 fallback: on a project holding no role rows at all, a tenant-visible
// caller acts as MAINTAINER so pre-migration projects keep working. Gating on the role
// alone would hide the only way to add a repository on every ungoverned project — the
// migration flag day §3 exists to avoid.
//
// So the predicate mirrors the gate exactly: `may()` (role or platform admin), OR the
// fallback. `ungoverned` is the SERVER's `ProjectDetail.ungoverned` bit, and only a
// literal `true` counts — `undefined` (a pre-hint response) is "not established", which
// fails closed the same way `emptyRosterReason` does.
// ---------------------------------------------------------------------------
export function mayMaintainProject(
  held: ProjectRoleName | null,
  roleLevel: number,
  ungoverned: boolean | null | undefined,
): boolean {
  return roleLevel >= 2 || meetsRole(held, 'maintainer') || ungoverned === true;
}

export function roleLabel(role: ProjectRoleName): string {
  return PROJECT_ROLE_OPTIONS.find((o) => o.value === role)?.label ?? role;
}

// ---------------------------------------------------------------------------
// isProjectRoleName — the wire `role` string is free-form on the read model, so
// narrow it once at the boundary instead of casting at every use site.
// ---------------------------------------------------------------------------
export function isProjectRoleName(v: string): v is ProjectRoleName {
  return v === 'viewer' || v === 'maintainer' || v === 'owner';
}

// ---------------------------------------------------------------------------
// Role badge tints. Keeps the ESTABLISHED privilege ramp (violet = the most
// privileged role, mirroring usersAdminForm's admin=violet) rather than reusing
// the Ops emerald/amber/rose status palette — a role is not a health state, and
// an emerald "Owner" would read as "healthy" next to a real status pill.
// Maintainer takes teal so the middle rank still sits in the Ops color family.
// ---------------------------------------------------------------------------
export const PROJECT_ROLE_BADGE: Record<ProjectRoleName, string> = {
  owner: 'bg-violet-50 text-violet-700',
  maintainer: 'bg-teal-50 text-teal-700',
  viewer: 'bg-slate-100 text-slate-600',
};

// ---------------------------------------------------------------------------
// projectRoleBadge — a role row's RAW wire `role` string → the tint AND the label its pill
// wears (E28/T10). One call instead of the narrow-then-branch ternary pair this had been
// inlined as at `ProjectAccessTab.tsx:519-523`.
//
// WHY IT IS WORTH EXTRACTING. The governance surface carries three copies of the equivalent
// `roleBadge` helper, and TWO of them label an unrecognized role **`Invoker`** — the
// low-privilege grant name — because they type their parameter as `'Invoker' | 'Admin'` and
// then treat "not Admin" as Invoker. The wire field is free-form (`client.ts` says so:
// "backend is tolerant of foreign role ids"), so that branch is reachable, and the value it
// invents is a PRIVILEGE. A pill that answers "Invoker" for a role it could not identify
// asserts a grant nobody made — the worst kind of value to get wrong on an access surface.
//
// The Ops equivalent happened to be correct, but it was an unpinned ternary in a `.tsx`
// (which vitest does not even collect), so nothing stopped it drifting the same way. Here it
// is a named function with tests, and the rule is explicit: an unidentified role gets the
// NEUTRAL slate tint and its own value echoed back, never a rank's color and never a rank's
// name.
//
// Echoing the raw string preserves what `ProjectAccessTab` already did — a foreign role id
// is real data an operator may need to see, so it is shown rather than replaced. Case
// -SENSITIVE, matching `isProjectRoleName`: the backend writes lowercase literals and the
// role gates compare them exactly, so tolerating `OWNER` here would make the badge claim a
// standing that `effectiveRole` — and `may()` server-side — would refuse.
// ---------------------------------------------------------------------------
export interface ProjectRoleBadge {
  /** The role, or 'unknown' when the wire value is not one of ours. */
  key: ProjectRoleName | 'unknown';
  /** What the pill reads. For an unknown role: the raw value, or 'Unknown' when blank. */
  label: string;
  /** The pill's Tailwind color pair. */
  cls: string;
}

export function projectRoleBadge(raw: string | null | undefined): ProjectRoleBadge {
  const value = (raw ?? '').trim();
  if (isProjectRoleName(value)) {
    return { key: value, label: roleLabel(value), cls: PROJECT_ROLE_BADGE[value] };
  }
  return {
    key: 'unknown',
    // Slate-500 rather than the `viewer` slate-600: legible, but visibly not a rank.
    cls: 'bg-slate-100 text-slate-500',
    label: value || 'Unknown',
  };
}

// ---------------------------------------------------------------------------
// effectiveRole — the caller's OWN role on this project, read off the SERVER'S
// answer (`GET /projects/{id}` → `effective_role`).
//
// This replaces the roster-derived guess T11 shipped with. The roster cannot
// answer the question: a role may be granted to an Entra GROUP, and nothing in
// the browser evaluates group membership (`/users/me` carries no group claim) —
// so a group-derived OWNER, which is the shape the groups-first design steers
// customers toward, has no direct row and looked identical to a role-less
// caller. The backend derives this from the ProjectContext it already resolved,
// and reports a PLATFORM ADMIN as 'owner' (their `may()` short-circuits True).
//
// Still not an authority — `may()` server-side is the gate; this only decides
// which affordances are worth rendering. Unknown/absent/garbage narrows to
// `null`, i.e. show nothing rather than guess (fail-closed, same as the gate).
// ---------------------------------------------------------------------------
export function effectiveRole(source: EffectiveRoleSource | null | undefined): ProjectRoleName | null {
  const raw = source?.effective_role;
  return typeof raw === 'string' && isProjectRoleName(raw) ? raw : null;
}

// ---------------------------------------------------------------------------
// mayOfferGrant — may the Grant trigger be RENDERED?
//
// Owner-or-admin is necessary but NOT sufficient: the picker's whole "can only ever
// ADD" premise rests on `existingIds`, and the backend POST is an UPSERT. A roster we
// failed to read yields an EMPTY `existingIds`, so a principal who already holds Owner
// would pass `filterNewPrincipals`, appear in the results, and a Viewer "grant" would
// SILENTLY DOWNGRADE them. So the affordance also requires that the roster is KNOWN —
// "loaded and empty" is a fact; "load failed" is not an empty roster.
//
// `rosterLoaded` is false while the read is in flight AND after it failed. Refusing to
// render (rather than disabling) matches the epic's FE constraint.
//
// This is only sound because the WIRE distinguishes the two cases. It previously did not:
// the roles route read the degrading loader, so a store fault answered 200 + `[]` and
// `rosterLoaded` went TRUE over a roster nobody had read — the same silent downgrade,
// through the other door. E27/T11 FIX 3 moved that route onto `list_for_project_strict`,
// so an unreadable partition is a 503 the catch below sees. Do NOT put the roles route
// back on the degrading read.
// ---------------------------------------------------------------------------
export function mayOfferGrant(
  held: ProjectRoleName | null,
  roleLevel: number,
  rosterLoaded: boolean,
): boolean {
  return rosterLoaded && canManageRoles(held, roleLevel);
}

// ---------------------------------------------------------------------------
// grantRefusal — belt-and-braces for the same upsert hazard, at the point of write.
//
// `mayOfferGrant` keeps the picker off screen while the roster is unknown, so a hit
// that already holds a role should be unreachable. If one arrives anyway (the roster
// moved under us between the search and the click), refuse rather than upsert: a
// "grant" that quietly rewrites an existing row is the one thing this surface promises
// it never does. Returns the sentence to show, or null when the grant is a true ADD.
// ---------------------------------------------------------------------------
export function grantRefusal(principalId: string, existingIds: Set<string>): string | null {
  return existingIds.has(principalId)
    ? 'That principal already holds a role on this project. Change it from its row in the list below — granting again would overwrite the role they already have.'
    : null;
}

// ---------------------------------------------------------------------------
// Empty-roster copy. An empty list is NOT evidence that a project is ungoverned.
// The roles route now reads the STRICT loader, so an unreadable partition surfaces as
// 503 rather than 200 + `[]` (E27/T11 FIX 3) — but "the list came back empty" still
// answers a different question from "is this project governed?": the roster is per-
// project role ROWS, and only the server's own `ungoverned` derivation accounts for the
// design-§3 fallback. Asserting "anyone in the tenant can maintain it" off list length
// alone would state something about BLAST RADIUS that the list never established.
//
// So the assertion is made ONLY from the server's authoritative `ProjectDetail.ungoverned`
// bit, and only when it is literally `true`.
//
// The admin case is explicit rather than inferred. The backend derives the bit as
// `not pctx.is_global and pctx.roles.get(id) is None`, so a PLATFORM ADMIN always reads
// `ungoverned: false` — even on a genuinely ungoverned project. Treating that `false` as
// "this project is governed" would be reading a value that was never about the project.
// An admin therefore gets copy that says the list is inconclusive, never a claim either way.
// ---------------------------------------------------------------------------
export type EmptyRosterReason = 'ungoverned' | 'unknown-admin' | 'unknown';

export function emptyRosterReason(
  ungoverned: boolean | null | undefined,
  isAdmin: boolean,
): EmptyRosterReason {
  // Only a literal `true` is evidence. `undefined` (a pre-hint or differently-shaped
  // response) and `false`-for-an-admin both mean "not established" — fail closed.
  if (ungoverned === true) return 'ungoverned';
  return isAdmin ? 'unknown-admin' : 'unknown';
}

export const EMPTY_ROSTER_COPY: Record<EmptyRosterReason, { headline: string; detail: string }> = {
  ungoverned: {
    headline: 'No roles granted yet.',
    detail:
      'Until someone holds a role here, anyone in the project’s tenant can maintain it.',
  },
  'unknown-admin': {
    headline: 'No role rows on this project.',
    detail:
      'You see this project in full as a platform administrator, so this list cannot tell you whether anyone else governs it. Grant Owner to a group to make ownership explicit.',
  },
  unknown: {
    headline: 'No roles returned.',
    detail:
      'The server does not report this project as ungoverned, so retry before concluding that nobody has access.',
  },
};

// ---------------------------------------------------------------------------
// sortRoleRows — roster order: authority descending (owners first), then groups
// before users at equal rank (the groups-first design reads better when the
// durable grants lead), then display name. Non-mutating.
// ---------------------------------------------------------------------------
export function sortRoleRows(rows: ProjectRoleRecord[]): ProjectRoleRecord[] {
  const rank = (r: ProjectRoleRecord): number => (isProjectRoleName(r.role) ? RANK[r.role] : -1);
  return [...rows].sort((a, b) => {
    if (rank(a) !== rank(b)) return rank(b) - rank(a);
    if (a.principal_type !== b.principal_type) return a.principal_type === 'group' ? -1 : 1;
    return (a.principal_display || a.principal_id).localeCompare(b.principal_display || b.principal_id);
  });
}

// ---------------------------------------------------------------------------
// filterNewPrincipals — hide principals who ALREADY hold a role on this project
// from the picker results. Shape borrowed from `usersAdminForm.filterNewPrincipals`
// (the Add-user modal's equivalent); re-declared here rather than imported so the
// Ops slice does not reach into the governance-admin slice for one predicate.
// ---------------------------------------------------------------------------
export function filterNewPrincipals<T extends { id: string }>(
  hits: T[],
  existingIds: Set<string>,
): T[] {
  return hits.filter((h) => !existingIds.has(h.id));
}

/** The principal ids already on the roster — the picker's `existingIds`. */
export function grantedPrincipalIds(rows: ProjectRoleRecord[]): Set<string> {
  return new Set(rows.map((r) => r.principal_id));
}

// ---------------------------------------------------------------------------
// roleActionMessage — the backend's FIXED error literals → a sentence that says
// what happened and what to do about it.
//
// The axios interceptor surfaces the backend `detail` as `err.message`, so these
// are matched on the literals `api/routes/projects.py` pins:
//   409 "project must keep at least one owner"   — the last-owner refusal
//   503 "could not verify project ownership"     — the roster was unreadable
//   403 "insufficient project role"              — the caller is not an owner
//   400 "invalid project role"                   — malformed id / unknown role
// Anything else passes through unchanged (it is already a curated literal), and
// an empty message falls back to `fallback`.
// ---------------------------------------------------------------------------
export function roleActionMessage(raw: string, fallback = 'The change could not be saved.'): string {
  const message = raw.trim();
  if (!message) return fallback;
  if (/must keep at least one owner/i.test(message)) {
    return 'This project must keep at least one owner. Grant Owner to someone else first, then change or remove this one.';
  }
  if (/could not verify project ownership/i.test(message)) {
    return 'Couldn’t verify this project’s owners just now, so the change was refused rather than risk leaving the project with none. Try again in a moment.';
  }
  if (/insufficient project role/i.test(message)) {
    return 'You need the Owner role on this project to change who has access.';
  }
  if (/invalid project role/i.test(message)) {
    return 'That role or principal isn’t valid for this project.';
  }
  return message;
}

// ---------------------------------------------------------------------------
// destructiveActionMessage — the same treatment for the two OWNER-gated DELETE
// paths, which share the 403 literal but not its remedy: `roleActionMessage`'s 403
// sentence talks about "changing who has access", which is the wrong sentence under a
// Delete button. Everything else (409 / 503 / 400 / pass-through / empty→fallback)
// delegates, so there is still ONE mapping table.
//
// Both routes are OWNER-gated server-side and the design-§3 ungoverned fallback stops
// at MAINTAINER, so it cannot supply either verb — hence "you need Owner", flatly.
// ---------------------------------------------------------------------------
export function destructiveActionMessage(
  raw: string,
  subject: 'project' | 'repository',
  fallback: string,
): string {
  const message = raw.trim();
  if (!message) return fallback;
  if (/insufficient project role/i.test(message)) {
    return subject === 'project'
      ? 'You need the Owner role on this project to delete it. Ask an owner to delete it, or to grant you Owner.'
      : 'You need the Owner role on this project to delete a repository — the teardown removes its runtime, images, identity and GitHub repo. Ask an owner to run it, or to grant you Owner.';
  }
  return roleActionMessage(message, fallback);
}

// ---------------------------------------------------------------------------
// maintainerActionMessage — the same treatment for the two MAINTAINER-gated repository
// verbs (create-from-template, retry-from-failed-step). They shared the 403 literal with
// the role and delete paths but routed it NOWHERE: a Viewer filled in the whole create
// form and read the raw lowercase fragment `insufficient project role`.
//
// Its own 403 sentence for the same reason `destructiveActionMessage` has one — both
// "to change who has access" and "to delete it" are the wrong sentence under a
// New-repository button — and it names MAINTAINER, not Owner, because that is the
// threshold these two routes actually gate on. Everything else delegates, so there is
// still ONE mapping table.
// ---------------------------------------------------------------------------
export function maintainerActionMessage(raw: string, fallback: string): string {
  const message = raw.trim();
  if (!message) return fallback;
  if (/insufficient project role/i.test(message)) {
    return 'You need the Maintainer role on this project to add or retry a repository. Ask an owner to grant you one.';
  }
  return roleActionMessage(message, fallback);
}

// ---------------------------------------------------------------------------
// promotionActionMessage — the promote route's FIXED literals (E27/T12). Promote
// carries FOUR literals no other verb has, so it needs its own branches rather than a
// pass-through:
//   403 "insufficient project role"          — promote is on the STRICT gate
//   409 "no prod candidate to promote"       — nothing is pending (E27A's narrowing)
//   409 "no dev deployment to promote"       — the PRE-E27A refusal. Unreachable for records
//        written since, retained because pre-E27A rows and their tests still produce it.
//   409 "a promotion is already in flight"   — a prod build is running
//   502 "failed to start the promotion build" — the build never STARTED
//   404 "Repository not found"               — the row moved under us
//
// Both 409s get their OWN sentence, and they must not be collapsed: they name different
// remedies ("merge to main" vs "ship to dev at all"), and the E27A one is the branch an
// operator will actually hit — a candidate that a concurrent promote consumed between this
// page's read and the click.
//
// Its 403 sentence is its own for the same reason the delete and maintainer paths have
// theirs — "to change who has access" is the wrong sentence under a Promote button — and
// it says the §3 ungoverned fallback does NOT apply here, because that is the one thing
// an operator on a pre-migration project will otherwise find inexplicable: they can add
// repositories without a role row, but they cannot ship to prod without one.
//
// The 502 sentence must distinguish "the build never started" from "the deploy failed":
// nothing reached production, so retrying is safe. Everything shared delegates, so there
// is still ONE mapping table.
// ---------------------------------------------------------------------------
export function promotionActionMessage(raw: string, fallback = 'The promotion could not be started.'): string {
  const message = raw.trim();
  if (!message) return fallback;
  if (/insufficient project role/i.test(message)) {
    return 'You need the Owner role on this project to promote to prod — shipping to production always needs a named owner, even on a project with no roles granted yet. Ask an owner to promote it, or to grant you Owner.';
  }
  if (/no prod candidate to promote/i.test(message)) {
    return 'There is nothing waiting for approval on this repository — either it has no merge to main yet, or the candidate was already promoted. Merge to main first; the merge that lands there becomes the next candidate.';
  }
  if (/no dev deployment to promote/i.test(message)) {
    return 'This repository hasn’t deployed to dev yet, so there is no image to promote. Push to its dev branch and let that build finish first.';
  }
  if (/promotion is already in flight/i.test(message)) {
    return 'A promotion is already running for this repository. Wait for it to finish — the status here updates on its own.';
  }
  if (/failed to start the promotion build/i.test(message)) {
    return 'The promotion build couldn’t be started, so nothing was deployed to prod. Try again in a moment.';
  }
  if (/repository not found/i.test(message)) {
    return 'This repository is no longer available — it may have been deleted. Reload the project.';
  }
  return roleActionMessage(message, fallback);
}
