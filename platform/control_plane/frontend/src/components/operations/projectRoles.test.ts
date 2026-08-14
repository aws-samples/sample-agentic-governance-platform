// projectRoles.test.ts
import { describe, expect, it } from 'vitest';
import type { ProjectRoleRecord } from '../../api/client';
import {
  EMPTY_ROSTER_COPY,
  canDestroy,
  canManageRoles,
  canPromote,
  cicdBadgeKey,
  destructiveActionMessage,
  effectiveRole,
  emptyRosterReason,
  filterNewPrincipals,
  grantRefusal,
  grantedPrincipalIds,
  isPromotionInFlight,
  keepPromotionOverride,
  promoteBlockedReason,
  maintainerActionMessage,
  mayMaintainProject,
  mayOfferGrant,
  meetsRole,
  prodCandidateView,
  promotionActionMessage,
  promotionStatusLabel,
  projectRoleBadge,
  roleActionMessage,
  roleLabel,
  sortRoleRows,
  PROJECT_ROLE_BADGE,
  PROJECT_ROLE_OPTIONS,
} from './projectRoles';
// The badge tints themselves — asserted through, so "promoting and deployed must not share
// a tint" is pinned against the real palette rather than against two key names that could
// both be re-pointed at amber.
import { OPS_BADGE } from './opsUi';
// The single exhaustive status tables (E28/T10). Imported so the two helpers below can be
// asserted to DELEGATE to them rather than to carry a second copy — the drift this epic's
// anti-drift task exists to make impossible.
import { CICD_BADGE_KEY, CICD_LABEL, toCicdStatus } from './opsStatus';

describe('meetsRole', () => {
  it('is a threshold, not an equality', () => {
    expect(meetsRole('maintainer', 'viewer')).toBe(true);
    expect(meetsRole('maintainer', 'maintainer')).toBe(true);
    expect(meetsRole('maintainer', 'owner')).toBe(false);
  });
  it('treats no grant as no access', () => {
    expect(meetsRole(null, 'viewer')).toBe(false);
  });
});

describe('canManageRoles', () => {
  it('allows owners', () => expect(canManageRoles('owner', 1)).toBe(true));
  it('allows platform admins regardless of project role', () => expect(canManageRoles(null, 2)).toBe(true));
  it('refuses maintainers', () => expect(canManageRoles('maintainer', 1)).toBe(false));
});

// canPromote's precondition is E27A's central change: promoting is no longer "ship whatever
// last reached dev", it is APPROVING A NAMED MERGE TO MAIN. So the third argument is the
// repo's `prod_candidate_status`, not its dev image tag — an owner staring at a repo whose
// dev build is green but whose main branch has moved nowhere has nothing to approve.
describe('canPromote', () => {
  it('requires owner AND a pending prod candidate', () => {
    expect(canPromote('owner', 1, 'pending')).toBe(true);
    expect(canPromote('owner', 1, null)).toBe(false);
    expect(canPromote('maintainer', 1, 'pending')).toBe(false);
  });
  it('lets a platform admin promote', () => expect(canPromote(null, 2, 'pending')).toBe(true));
  it('accepts ONLY the literal "pending" status', () => {
    // Unlike `cicd_status` this value has ONE writer — `record_prod_candidate`, which sets the
    // literal "pending" — and the promote route CLEARS all five fields on a successful start.
    // So it is compared exactly: anything else is not a candidate awaiting approval, and
    // guessing would offer the button over a candidate that has already shipped.
    expect(canPromote('owner', 1, 'promoted')).toBe(false);
    expect(canPromote('owner', 1, '')).toBe(false);
    expect(canPromote('owner', 1, 'Pending')).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// prodCandidateView — the ROW's answer to "what exactly am I approving?". The whole point of
// the E27A row is that an owner reads the merge, the author and the image BEFORE clicking, so
// every piece of that derivation is pinned here rather than inlined in the `.tsx` (which
// vitest never collects).
// ---------------------------------------------------------------------------
describe('prodCandidateView', () => {
  const CANDIDATE = {
    prod_candidate_status: 'pending',
    prod_candidate_sha: '3f9a1c2e8d7b6a5f4e3d2c1b0a9f8e7d6c5b4a39',
    prod_candidate_actor: 'jorge',
    prod_candidate_image_tag: 'agent-7-3f9a1c2',
  };

  it('truncates the FULL sha to seven and prefixes the actor with @', () => {
    // The wire carries the full 40-char sha; seven is what a reviewer reads and what the
    // image tag already embeds, so the two line up visually on the row.
    const view = prodCandidateView(CANDIDATE);
    expect(view?.shortSha).toBe('3f9a1c2');
    expect(view?.actor).toBe('@jorge');
    expect(view?.imageTag).toBe('agent-7-3f9a1c2');
  });

  it('renders NOTHING without a pending candidate', () => {
    // Conditionally rendered, never disabled — so "no candidate" has to be representable as
    // the absence of the whole line, not as an empty one.
    expect(prodCandidateView({})).toBeNull();
    expect(prodCandidateView({ prod_candidate_status: null })).toBeNull();
    expect(prodCandidateView(undefined)).toBeNull();
    expect(prodCandidateView(null)).toBeNull();
  });

  it('renders nothing for a NON-pending status, even with the other fields present', () => {
    // The promote route CLEARS all five fields on a successful start, so fields surviving a
    // cleared status mean a partially-written row — which must not read as approvable.
    expect(prodCandidateView({ ...CANDIDATE, prod_candidate_status: 'promoted' })).toBeNull();
    expect(prodCandidateView({ ...CANDIDATE, prod_candidate_status: 'Pending' })).toBeNull();
  });

  it('agrees with canPromote about WHEN a candidate exists', () => {
    // The row and the button must never disagree: an owner must not read "awaiting your
    // approval" over a row with no button, nor see a button with nothing described above it.
    for (const status of [null, undefined, '', 'pending', 'Pending', 'promoted']) {
      expect(prodCandidateView({ ...CANDIDATE, prod_candidate_status: status }) !== null).toBe(
        canPromote('owner', 1, status ?? null)
      );
    }
  });

  it('drops a missing sha/actor/tag to null rather than rendering a stub', () => {
    // A pending candidate is written by ONE route that always supplies all five, so these are
    // defence against a partial row — an empty `@` or a bare `main @` would be worse than
    // silence on the one line whose job is naming what ships.
    const view = prodCandidateView({ prod_candidate_status: 'pending' });
    expect(view).not.toBeNull();
    expect(view?.shortSha).toBeNull();
    expect(view?.actor).toBeNull();
    expect(view?.imageTag).toBeNull();
  });

  it('leaves an already-short sha alone and trims whitespace', () => {
    expect(prodCandidateView({ prod_candidate_status: 'pending', prod_candidate_sha: 'abc12' })?.shortSha).toBe(
      'abc12'
    );
    expect(
      prodCandidateView({ prod_candidate_status: 'pending', prod_candidate_actor: '  jorge ' })?.actor
    ).toBe('@jorge');
  });

  it('does not double-prefix an actor that already carries an @', () => {
    expect(prodCandidateView({ prod_candidate_status: 'pending', prod_candidate_actor: '@jorge' })?.actor).toBe(
      '@jorge'
    );
  });
});

// effectiveRole — the SERVER's answer to "what do I hold here?", off the detail read.
// The browser cannot derive it: a role may be granted to an Entra GROUP, and nothing
// client-side evaluates group membership. It is a UI hint, never an authority.
describe('effectiveRole', () => {
  it('reads the three role names through', () => {
    expect(effectiveRole({ effective_role: 'viewer' })).toBe('viewer');
    expect(effectiveRole({ effective_role: 'maintainer' })).toBe('maintainer');
    expect(effectiveRole({ effective_role: 'owner' })).toBe('owner');
  });
  it('maps no role to null', () => {
    expect(effectiveRole({ effective_role: null })).toBeNull();
    expect(effectiveRole({})).toBeNull();
    expect(effectiveRole(undefined)).toBeNull();
    expect(effectiveRole(null)).toBeNull();
  });
  it('narrows an unknown value to null rather than guessing', () => {
    // Fail-closed: an unrecognised wire value shows NOTHING, matching the backend gate's
    // stance on an unrecognised stored role name.
    expect(effectiveRole({ effective_role: 'admin' })).toBeNull();
    expect(effectiveRole({ effective_role: '' })).toBeNull();
  });
  it('carries a group-derived owner that the roster could never show', () => {
    // The whole reason the field exists — no direct row, owner via a group.
    expect(canManageRoles(effectiveRole({ effective_role: 'owner' }), 1)).toBe(true);
  });
  it('treats the backend’s admin-as-owner report as owner', () => {
    // The backend reports a PLATFORM ADMIN as 'owner' (their may() short-circuits True),
    // so an admin keeps their own buttons even with no role row anywhere.
    expect(effectiveRole({ effective_role: 'owner' })).toBe('owner');
    expect(canManageRoles(effectiveRole({ effective_role: 'owner' }), 0)).toBe(true);
    expect(canPromote(effectiveRole({ effective_role: 'owner' }), 0, 'pending')).toBe(true);
  });
  it('does not hand a maintainer owner affordances', () => {
    // The regression T11 shipped with: a maintainer on a project carrying ANY group grant
    // used to see Grant/Revoke and get a 403 on click.
    expect(canManageRoles(effectiveRole({ effective_role: 'maintainer' }), 1)).toBe(false);
    expect(canPromote(effectiveRole({ effective_role: 'maintainer' }), 1, 'pending')).toBe(false);
  });
  it('shows nothing when the caller holds no role', () => {
    expect(canManageRoles(effectiveRole({ effective_role: null }), 1)).toBe(false);
    expect(canPromote(effectiveRole({ effective_role: null }), 1, 'pending')).toBe(false);
  });
});

describe('PROJECT_ROLE_OPTIONS', () => {
  it('exposes exactly the three roles in ascending authority', () => {
    expect(PROJECT_ROLE_OPTIONS.map((o) => o.value)).toEqual(['viewer', 'maintainer', 'owner']);
  });
  it('labels round-trip', () => expect(roleLabel('owner')).toBe('Owner'));
});

// ---------------------------------------------------------------------------
// Test helpers — a minimal ProjectRoleRecord factory. `role`/`principal_type` are
// bare `string` on the wire read-model, which is exactly what the helpers must
// tolerate, so the factory does not narrow them.
// ---------------------------------------------------------------------------
function row(over: Partial<ProjectRoleRecord> & { principal_id: string }): ProjectRoleRecord {
  return {
    project_id: 'p1',
    principal_type: 'user',
    principal_display: '',
    role: 'viewer',
    granted_by: 'someone',
    granted_at: '2026-07-27T00:00:00Z',
    ...over,
  };
}

// ---------------------------------------------------------------------------
// canDestroy — the OWNER-gated destructive verbs (delete project / delete repository,
// the irreversible E23 cascade). Both routes gate on OWNER server-side and the §3
// ungoverned fallback stops at MAINTAINER, so it never supplies either.
// ---------------------------------------------------------------------------
describe('canDestroy', () => {
  it('offers the destructive verbs to an owner', () => {
    expect(canDestroy('owner', 1)).toBe(true);
  });
  it('refuses a maintainer — the routes are OWNER-gated, so the button would 403', () => {
    expect(canDestroy('maintainer', 1)).toBe(false);
    expect(canDestroy('viewer', 1)).toBe(false);
  });
  it('refuses a caller holding no role', () => {
    expect(canDestroy(null, 1)).toBe(false);
  });
  it('lets a platform admin through, matching may()’s is_global short-circuit', () => {
    expect(canDestroy(null, 2)).toBe(true);
  });
  it('reads the server hint end-to-end', () => {
    // The one signal the browser cannot compute — a group-derived owner has no row.
    expect(canDestroy(effectiveRole({ effective_role: 'owner' }), 0)).toBe(true);
    expect(canDestroy(effectiveRole({ effective_role: 'maintainer' }), 0)).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// mayMaintainProject — the two MAINTAINER-gated repository verbs (create-from-template,
// retry-from-failed-step). Unlike the OWNER verbs these route through
// `_require_project_role_or_ungoverned`, so the design-§3 fallback DOES reach them: on a
// project with no role rows a tenant-visible caller may still add a repository. Gating on
// the role alone would hide the only way to add one on every pre-migration project.
// ---------------------------------------------------------------------------
describe('mayMaintainProject', () => {
  it('offers the verbs to a maintainer and an owner', () => {
    expect(mayMaintainProject('maintainer', 1, false)).toBe(true);
    expect(mayMaintainProject('owner', 1, false)).toBe(true);
  });
  it('refuses a viewer on a GOVERNED project — the route would 403', () => {
    expect(mayMaintainProject('viewer', 1, false)).toBe(false);
    expect(mayMaintainProject(null, 1, false)).toBe(false);
  });
  it('honours the §3 fallback: an ungoverned project admits a role-less caller', () => {
    // This is the case a role-only gate would break — the migration flag day §3 avoids.
    expect(mayMaintainProject(null, 1, true)).toBe(true);
    expect(mayMaintainProject('viewer', 1, true)).toBe(true);
  });
  it('counts only a LITERAL true as ungoverned', () => {
    // `undefined` is a pre-hint / differently-shaped response — not established, fail closed.
    expect(mayMaintainProject(null, 1, undefined)).toBe(false);
    expect(mayMaintainProject(null, 1, null)).toBe(false);
  });
  it('lets a platform admin through regardless of either input', () => {
    expect(mayMaintainProject(null, 2, false)).toBe(true);
  });
  it('is NOT the same threshold as the destructive verbs', () => {
    // A maintainer may add a repo; they may not delete one. Same page, two thresholds.
    expect(mayMaintainProject('maintainer', 1, false)).toBe(true);
    expect(canDestroy('maintainer', 1)).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// mayOfferGrant — the Grant trigger needs authority AND a KNOWN roster. The backend
// POST is an upsert, so offering it over a roster we failed to read turns "grant
// Viewer" into a silent downgrade of whoever already holds Owner.
// ---------------------------------------------------------------------------
describe('mayOfferGrant', () => {
  it('offers Grant to an owner once the roster loaded', () => {
    expect(mayOfferGrant('owner', 1, true)).toBe(true);
  });
  it('WITHHOLDS Grant from an owner while the roster is unknown', () => {
    // The I-2 defect: the roster read failed, `existingIds` is empty, and the upsert
    // would silently downgrade an existing owner. A failed read is not an empty roster.
    expect(mayOfferGrant('owner', 1, false)).toBe(false);
  });
  it('withholds Grant from a platform admin too while the roster is unknown', () => {
    // Authority does not make the upsert safe — the missing input is the ROSTER.
    expect(mayOfferGrant(null, 2, false)).toBe(false);
    expect(mayOfferGrant(null, 2, true)).toBe(true);
  });
  it('still refuses a maintainer even with a loaded roster', () => {
    expect(mayOfferGrant('maintainer', 1, true)).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// grantRefusal — the write-side half of the same hazard.
// ---------------------------------------------------------------------------
describe('grantRefusal', () => {
  it('refuses a principal who already holds a role rather than upserting over it', () => {
    const message = grantRefusal('g-1', new Set(['g-1']));
    expect(message).not.toBeNull();
    expect(message).toMatch(/already holds a role/i);
    // It must point at the remedy (the row's own select), not just refuse.
    expect(message).toMatch(/row/i);
  });
  it('allows a genuine ADD', () => {
    expect(grantRefusal('g-2', new Set(['g-1']))).toBeNull();
  });
  it('allows everything when the roster is empty — an empty set is only ever safe once loaded', () => {
    expect(grantRefusal('g-1', new Set())).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// emptyRosterReason — "anyone in the tenant can maintain it" is a claim about BLAST
// RADIUS, so it may only be made from the server's authoritative `ungoverned` bit.
// An empty list is not evidence: the route's read swallows a store ClientError and
// returns [], so a DDB fault yields HTTP 200 + [] on a fully governed project.
// ---------------------------------------------------------------------------
describe('emptyRosterReason', () => {
  it('asserts ungoverned only when the SERVER says so', () => {
    expect(emptyRosterReason(true, false)).toBe('ungoverned');
  });
  it('does NOT assert ungoverned when the server says false', () => {
    // The store-fault case: 200 + [] on a governed project. Saying "anyone in the tenant
    // can maintain it" here would be false, and false about blast radius.
    expect(emptyRosterReason(false, false)).toBe('unknown');
  });
  it('treats an absent bit as unknown rather than guessing', () => {
    expect(emptyRosterReason(undefined, false)).toBe('unknown');
    expect(emptyRosterReason(null, false)).toBe('unknown');
  });
  it('never infers governance from the bit FOR AN ADMIN (spec S-2)', () => {
    // The backend derives it as `not is_global and roles.get(id) is None`, so an admin
    // reads false even on a genuinely ungoverned project — the value is not about the
    // project at all. Handled explicitly, not folded into either claim.
    expect(emptyRosterReason(false, true)).toBe('unknown-admin');
    expect(emptyRosterReason(undefined, true)).toBe('unknown-admin');
  });
  it('still honours an explicit true for an admin', () => {
    expect(emptyRosterReason(true, true)).toBe('ungoverned');
  });
  it('only the ungoverned copy claims tenant-wide maintain rights', () => {
    expect(EMPTY_ROSTER_COPY.ungoverned.detail).toMatch(/anyone in the project’s tenant/);
    expect(EMPTY_ROSTER_COPY.unknown.detail).not.toMatch(/anyone in the/);
    expect(EMPTY_ROSTER_COPY['unknown-admin'].detail).not.toMatch(/anyone in the/);
    for (const copy of Object.values(EMPTY_ROSTER_COPY)) {
      expect(copy.headline.length).toBeGreaterThan(0);
      expect(copy.detail.length).toBeGreaterThan(0);
    }
  });
});

// ---------------------------------------------------------------------------
// sortRoleRows — roster order: authority desc → groups before users at equal rank →
// display name. Non-mutating by contract.
// ---------------------------------------------------------------------------
describe('sortRoleRows', () => {
  it('puts the highest authority first', () => {
    const sorted = sortRoleRows([
      row({ principal_id: 'v', role: 'viewer' }),
      row({ principal_id: 'o', role: 'owner' }),
      row({ principal_id: 'm', role: 'maintainer' }),
    ]);
    expect(sorted.map((r) => r.principal_id)).toEqual(['o', 'm', 'v']);
  });
  it('puts groups before users at EQUAL rank (the durable grants lead)', () => {
    const sorted = sortRoleRows([
      row({ principal_id: 'u', role: 'owner', principal_type: 'user', principal_display: 'AAA' }),
      row({ principal_id: 'g', role: 'owner', principal_type: 'group', principal_display: 'ZZZ' }),
    ]);
    // Group wins despite sorting later by display name — so rank/type beat the name.
    expect(sorted.map((r) => r.principal_id)).toEqual(['g', 'u']);
  });
  it('breaks a full tie on display name', () => {
    const sorted = sortRoleRows([
      row({ principal_id: 'b', role: 'viewer', principal_display: 'Beta' }),
      row({ principal_id: 'a', role: 'viewer', principal_display: 'Alpha' }),
    ]);
    expect(sorted.map((r) => r.principal_id)).toEqual(['a', 'b']);
  });
  it('falls back to the principal id when a display name is blank', () => {
    // A row whose display name never resolved must still sort deterministically —
    // by its id — rather than collapsing to the empty string and sorting first.
    const sorted = sortRoleRows([
      row({ principal_id: 'zzz-no-display', role: 'viewer', principal_display: '' }),
      row({ principal_id: 'ignored', role: 'viewer', principal_display: 'Alpha' }),
    ]);
    expect(sorted.map((r) => r.principal_id)).toEqual(['ignored', 'zzz-no-display']);
  });
  it('sinks an UNKNOWN wire role to last instead of ranking it as viewer', () => {
    const sorted = sortRoleRows([
      row({ principal_id: 'weird', role: 'superuser' }),
      row({ principal_id: 'v', role: 'viewer' }),
    ]);
    expect(sorted.map((r) => r.principal_id)).toEqual(['v', 'weird']);
  });
  it('does NOT mutate its input', () => {
    const input = [
      row({ principal_id: 'v', role: 'viewer' }),
      row({ principal_id: 'o', role: 'owner' }),
    ];
    const before = input.map((r) => r.principal_id);
    const sorted = sortRoleRows(input);
    expect(input.map((r) => r.principal_id)).toEqual(before);
    expect(sorted).not.toBe(input);
  });
});

// ---------------------------------------------------------------------------
// grantedPrincipalIds + filterNewPrincipals — together they ARE the picker's
// "can only ever ADD" mechanism, so both must be pinned: a no-op version of either
// re-opens the silent-downgrade path.
// ---------------------------------------------------------------------------
describe('grantedPrincipalIds', () => {
  it('collects every principal id on the roster', () => {
    const ids = grantedPrincipalIds([row({ principal_id: 'a' }), row({ principal_id: 'b' })]);
    expect(ids.has('a')).toBe(true);
    expect(ids.has('b')).toBe(true);
    expect(ids.size).toBe(2);
  });
  it('is empty for an empty roster', () => {
    expect(grantedPrincipalIds([]).size).toBe(0);
  });
});

describe('filterNewPrincipals', () => {
  it('drops hits that already hold a role and keeps the rest', () => {
    const hits = [{ id: 'a' }, { id: 'b' }, { id: 'c' }];
    expect(filterNewPrincipals(hits, new Set(['b'])).map((h) => h.id)).toEqual(['a', 'c']);
  });
  it('drops EVERYTHING when every hit already holds a role', () => {
    const hits = [{ id: 'a' }, { id: 'b' }];
    expect(filterNewPrincipals(hits, new Set(['a', 'b']))).toEqual([]);
  });
  it('passes everything through against an empty set', () => {
    const hits = [{ id: 'a' }];
    expect(filterNewPrincipals(hits, new Set()).map((h) => h.id)).toEqual(['a']);
  });
  it('round-trips against the roster it is derived from', () => {
    // The real wiring: existingIds comes from grantedPrincipalIds(rows), so a principal
    // already on the roster can never appear in the picker.
    const rows = [row({ principal_id: 'held' })];
    const hits = [{ id: 'held' }, { id: 'fresh' }];
    expect(filterNewPrincipals(hits, grantedPrincipalIds(rows)).map((h) => h.id)).toEqual(['fresh']);
  });
});

// ---------------------------------------------------------------------------
// roleActionMessage — the whole error-UX contract. Each of the backend's four FIXED
// literals maps to a sentence; nothing lowercase-internal reaches the DOM.
// ---------------------------------------------------------------------------
describe('roleActionMessage', () => {
  it('explains the 409 last-owner refusal AND the remedy', () => {
    const out = roleActionMessage('project must keep at least one owner');
    expect(out).toMatch(/must keep at least one owner/i);
    expect(out).toMatch(/grant owner to someone else/i);
  });
  it('explains the 503 as REFUSED, not failed, and as retryable', () => {
    const out = roleActionMessage('could not verify project ownership');
    expect(out).toMatch(/refused/i);
    expect(out).toMatch(/try again/i);
  });
  it('turns the 403 into a sentence naming the role needed', () => {
    expect(roleActionMessage('insufficient project role')).toMatch(/need the Owner role/i);
  });
  it('turns the 400 into a sentence', () => {
    expect(roleActionMessage('invalid project role')).toMatch(/isn’t valid for this project/i);
  });
  it('never returns a raw mapped literal', () => {
    for (const literal of [
      'project must keep at least one owner',
      'could not verify project ownership',
      'insufficient project role',
      'invalid project role',
    ]) {
      expect(roleActionMessage(literal)).not.toBe(literal);
    }
  });
  it('matches case-insensitively, so a wrapped literal still maps', () => {
    expect(roleActionMessage('Insufficient Project Role')).toMatch(/need the Owner role/i);
  });
  it('falls back only on an empty/whitespace message', () => {
    expect(roleActionMessage('', 'Could not load.')).toBe('Could not load.');
    expect(roleActionMessage('   ', 'Could not load.')).toBe('Could not load.');
  });
  it('passes an unmapped message through rather than swallowing it', () => {
    expect(roleActionMessage('Network Error', 'Could not load.')).toBe('Network Error');
  });
});

// ---------------------------------------------------------------------------
// destructiveActionMessage — same table, but the 403 needs a DELETE-shaped remedy:
// "to change who has access" is the wrong sentence under a Delete button.
// ---------------------------------------------------------------------------
describe('destructiveActionMessage', () => {
  it('names deleting the PROJECT in its 403, not role management', () => {
    const out = destructiveActionMessage('insufficient project role', 'project', 'fallback');
    expect(out).toMatch(/Owner role on this project to delete it/i);
    expect(out).not.toMatch(/who has access/i);
  });
  it('names the REPOSITORY teardown in its 403', () => {
    const out = destructiveActionMessage('insufficient project role', 'repository', 'fallback');
    expect(out).toMatch(/delete a repository/i);
    // The cascade is irreversible — say what is destroyed.
    expect(out).toMatch(/runtime/i);
  });
  it('delegates every other literal to the one mapping table', () => {
    expect(destructiveActionMessage('project must keep at least one owner', 'project', 'f')).toBe(
      roleActionMessage('project must keep at least one owner')
    );
  });
  it('falls back on empty and passes an unmapped message through', () => {
    expect(destructiveActionMessage('', 'project', 'Failed to delete the project.')).toBe(
      'Failed to delete the project.'
    );
    expect(destructiveActionMessage('Network Error', 'project', 'f')).toBe('Network Error');
  });
  it('never surfaces the raw 403 fragment for either subject', () => {
    for (const subject of ['project', 'repository'] as const) {
      expect(destructiveActionMessage('insufficient project role', subject, 'f')).not.toBe(
        'insufficient project role'
      );
    }
  });
});

// ---------------------------------------------------------------------------
// maintainerActionMessage — the two MAINTAINER verbs share the 403 literal with the role
// and delete paths but not its remedy: they need MAINTAINER, not Owner, and neither
// "who has access" nor "to delete it" is the sentence under a New-repository button.
// ---------------------------------------------------------------------------
describe('maintainerActionMessage', () => {
  it('names the MAINTAINER threshold in its 403, not Owner', () => {
    const out = maintainerActionMessage('insufficient project role', 'f');
    expect(out).toMatch(/Maintainer role/i);
    expect(out).not.toMatch(/who has access/i);
    expect(out).not.toMatch(/to delete/i);
  });
  it('never surfaces the raw 403 fragment — the defect it exists to fix', () => {
    expect(maintainerActionMessage('insufficient project role', 'f')).not.toBe(
      'insufficient project role'
    );
    // Case-insensitive, like every other branch in the table.
    expect(maintainerActionMessage('Insufficient Project Role', 'f')).toMatch(/Maintainer role/i);
  });
  it('delegates every other literal to the one mapping table', () => {
    expect(maintainerActionMessage('could not verify project ownership', 'f')).toBe(
      roleActionMessage('could not verify project ownership')
    );
  });
  it('falls back on empty and passes an unmapped message through', () => {
    expect(maintainerActionMessage('', 'Failed to create the repository.')).toBe(
      'Failed to create the repository.'
    );
    expect(maintainerActionMessage('Network Error', 'f')).toBe('Network Error');
  });
});

// ---------------------------------------------------------------------------
// promoteBlockedReason — the two refusals `canPromote` folds into one `false`. "Not yet"
// (no pending candidate) earns a sentence; "not you" (not an owner) must show NOTHING.
// ---------------------------------------------------------------------------
describe('promoteBlockedReason', () => {
  it('agrees with canPromote on the allow case', () => {
    expect(promoteBlockedReason('owner', 1, 'pending')).toBe('ok');
    expect(canPromote('owner', 1, 'pending')).toBe(true);
  });
  it('separates “not yet” from “not you”', () => {
    // An owner with no candidate: a precondition they can act on (merge to main) → explain it.
    expect(promoteBlockedReason('owner', 1, null)).toBe('no-candidate');
    // A maintainer WITH a pending candidate: a role refusal → show nothing.
    expect(promoteBlockedReason('maintainer', 1, 'pending')).toBe('not-owner');
  });
  it('reports the ROLE refusal first when both apply', () => {
    // A viewer on a repo with no candidate must not be told to merge to main — the reason
    // they see no button is their role, and the other sentence would be a false lead.
    expect(promoteBlockedReason('viewer', 1, null)).toBe('not-owner');
    expect(promoteBlockedReason(null, 0, null)).toBe('not-owner');
  });
  it('lets a platform admin through, matching canPromote', () => {
    expect(promoteBlockedReason(null, 2, 'pending')).toBe('ok');
    expect(promoteBlockedReason(null, 2, null)).toBe('no-candidate');
  });
  it('treats an absent status field the same as a null one', () => {
    // The wire field is optional, so `undefined` must not read as a promotable candidate.
    expect(promoteBlockedReason('owner', 1, undefined)).toBe('no-candidate');
    expect(promoteBlockedReason('owner', 1, '')).toBe('no-candidate');
  });
  it('does NOT treat a promotable DEV IMAGE as a candidate — the E27A narrowing', () => {
    // The regression this rename exists to make impossible. A repo whose dev build shipped
    // an image but whose `main` has not moved has NOTHING to approve, and the backend now
    // 409s with "no prod candidate to promote". Offering the button over a stale dev tag
    // would put an owner one click from a refusal they cannot explain.
    expect(promoteBlockedReason('owner', 1, 'a-1-abc')).toBe('no-candidate');
  });
  it('never says ok where canPromote says false, for every combination', () => {
    // The two must not drift: 'ok' is EXACTLY canPromote === true. The status column sweeps
    // the real wire values AND a dev-image-shaped string, so a body that went back to a
    // truthiness check would fail here rather than only in the UI.
    for (const held of [null, 'viewer', 'maintainer', 'owner'] as const) {
      for (const level of [0, 1, 2]) {
        for (const status of [null, '', 'pending', 'Pending', 'promoted', 'a-1-abc']) {
          expect(promoteBlockedReason(held, level, status) === 'ok').toBe(
            canPromote(held, level, status)
          );
        }
      }
    }
  });
  it('reads the server hint end-to-end — a group-derived owner may promote', () => {
    expect(promoteBlockedReason(effectiveRole({ effective_role: 'owner' }), 0, 'pending')).toBe('ok');
    expect(promoteBlockedReason(effectiveRole({ effective_role: 'maintainer' }), 0, 'pending')).toBe(
      'not-owner'
    );
  });
});

// ---------------------------------------------------------------------------
// promotionStatusLabel — the repo row's cicd_status → the sentence-case label.
//
// E28/T10: this now DELEGATES to `opsStatus` — it narrows the bare wire string through
// `toCicdStatus` and reads the exhaustive `CICD_LABEL` table. The cases below therefore
// pin the DELEGATION and the two deliberate behaviour changes that came with it; the
// table's own exhaustiveness is `tsc`'s job and `opsStatus.test.ts`'s.
// ---------------------------------------------------------------------------
describe('promotionStatusLabel', () => {
  it('names the promoting state', () => expect(promotionStatusLabel('promoting')).toBe('Promoting to prod…'));
  it('maps terminal states', () => {
    expect(promotionStatusLabel('deployed')).toBe('Deployed');
    // "Delivery failed", not a bare "Failed": the runtime machine has its own `failed`, and the
    // repo detail header renders the two pills as an uncaptioned pair where two "Failed"s would
    // make an operator guess which half of the system broke.
    expect(promotionStatusLabel('failed')).toBe('Delivery failed');
  });

  it('says an unknown value is UNKNOWN, rather than naming a plausible state', () => {
    // CHANGED in E28/T10, deliberately. This used to answer 'Provisioning' for anything it
    // did not recognize — a confident sentence about a repo whose state nobody had
    // established, and the identical failure mode to the amber-prod bug that motivated the
    // closed union. An unrecognized value now reads as not-established.
    expect(promotionStatusLabel('weird')).toBe('No status reported');
    expect(promotionStatusLabel('')).toBe('No status reported');
    expect(promotionStatusLabel('weird')).not.toBe('Provisioning');
  });

  it('labels the materialize statuses, with `ready` no longer reading as shipped', () => {
    expect(promotionStatusLabel('provisioning')).toBe('Provisioning');
    // CHANGED in E28/T10. `ready` is what materialize's `_finalize_repo` writes — the repo is
    // scaffolded and nothing has been built or deployed — and the old emerald "Ready" pill read
    // as a terminal success over a repo that had never shipped anything. It is now a first-class
    // union member (fix round 1 corrected the pin, so nothing is translated) labelled for what
    // it denotes: the span from the end of materialize until the first build lands.
    //
    // SHORTENED in E28/T13 (M-f) from "Awaiting first build" — it was the widest pill AND every
    // fresh repo's default, so a new project's list was a column of the longest label in the
    // palette. `opsStatus.test.ts` holds the assertions about the wording's meaning; this one
    // pins that the wrapper still reads the same table.
    expect(promotionStatusLabel('ready')).toBe('Not built yet');
  });

  it('never leaks the raw lowercase wire value for a KNOWN status', () => {
    for (const status of ['promoting', 'deployed', 'failed', 'provisioning', 'ready']) {
      expect(promotionStatusLabel(status)).not.toBe(status);
      // Sentence-case: the pill used to render the bare wire string.
      expect(promotionStatusLabel(status)).toMatch(/^[A-Z]/);
    }
  });

  it('agrees with `opsStatus` — it is the same table, not a second one', () => {
    // The anti-drift assertion. If this helper ever grows its own table again, these stop
    // matching. That is the entire point of E28/T10.
    for (const raw of ['promoting', 'deployed', 'failed', 'provisioning', 'ready', 'weird', '']) {
      expect(promotionStatusLabel(raw)).toBe(CICD_LABEL[toCicdStatus(raw)]);
    }
  });
});

// ---------------------------------------------------------------------------
// cicdBadgeKey — the SAME cicd_status → the pill's color key, for BOTH tables that
// render one (the project's repositories tab and the global /ops/repositories list).
//
// This describe exists because the table used to be DUPLICATED, once per page, and only
// one copy was extended when E27 added `promoting` / `deployed`: on the other page both
// fell through to `pending`, so a repo LIVE IN PRODUCTION wore the same amber as one still
// provisioning. `cicd_status` has no enum, so no backend test can catch that — the sweep
// below over every status a backend writer actually produces is the only guard there is.
//
// The five writers, enumerated from the source: `provisioning` (the initial row + the retry
// reset, project_service), `ready` (materialize's `finalize`), `promoting` (the promote
// route), `deployed` (the buildspec's terminal `_st deployed`), `failed` (a failed
// materialize step, a mark-failed, or a promotion that could not start).
// ---------------------------------------------------------------------------
describe('cicdBadgeKey', () => {
  // Every status any backend writer can produce, and the tint it MUST wear.
  //
  // E28/T10 changed ONE row of this table: `ready` was `'ready'` (emerald) and is now
  // `'pending'` (amber). `ready` is materialize's `finalize` — the repo is scaffolded and
  // has never built or deployed — so the emerald terminal-success tint was making the same
  // kind of claim the amber-prod bug made, in the other direction.
  const WRITTEN: [string, string][] = [
    ['provisioning', 'provisioning'],
    ['ready', 'pending'],
    ['promoting', 'provisioning'],
    ['deployed', 'ready'],
    ['failed', 'failed'],
  ];

  it('maps every status the backend actually writes', () => {
    for (const [status, key] of WRITTEN) {
      expect(cicdBadgeKey(status)).toBe(key);
    }
  });

  it('never lets a REAL status wear the NOT-ESTABLISHED tint', () => {
    // The fall-through is now `unknown` (slate), not `pending` (amber) — a value nobody
    // recognized no longer borrows provisioning's color, which is what made a repo live in
    // production indistinguishable from one still being built.
    for (const [status] of WRITTEN) {
      expect(cicdBadgeKey(status)).not.toBe('unknown');
    }
  });

  it('gives deployed the TERMINAL-success tint and promoting the IN-FLIGHT one', () => {
    // The distinction the whole finding is about: `deployed` means prod is serving this
    // image; `promoting` means it is not yet. They must not share a tint.
    expect(cicdBadgeKey('deployed')).toBe('ready');
    expect(cicdBadgeKey('promoting')).toBe('provisioning');
    expect(OPS_BADGE[cicdBadgeKey('deployed')]).not.toBe(OPS_BADGE[cicdBadgeKey('promoting')]);
    // …and promoting must read like provisioning, not like success.
    expect(OPS_BADGE[cicdBadgeKey('promoting')]).toBe(OPS_BADGE[cicdBadgeKey('provisioning')]);
  });

  it('DROPPED the invented aliases no writer ever produces', () => {
    // E28/T10. The old table carried defensive branches for `ok` / `passing` / `success` /
    // `error` / `running`, which look like prudence but are guesses: nothing in this
    // codebase writes any of them. Under a closed union a guess is the failure mode — so
    // they now read as not-established, like any other unrecognized value.
    //
    // Fix round 1 extended the same reasoning to `building` and `pending`, which had been
    // union MEMBERS in the first draft of the pin. Nothing writes those either, so a member for
    // them made the exhaustiveness guard vouch for states that cannot occur.
    for (const invented of ['ok', 'passing', 'success', 'error', 'running', 'building', 'pending']) {
      expect(cicdBadgeKey(invented)).toBe('unknown');
    }
    // Every value a writer DOES produce keeps a real tint.
    expect(cicdBadgeKey('provisioning')).toBe('provisioning');
    expect(cicdBadgeKey('ready')).toBe('pending');
    expect(cicdBadgeKey('promoting')).toBe('provisioning');
    expect(cicdBadgeKey('deployed')).toBe('ready');
    expect(cicdBadgeKey('failed')).toBe('failed');
  });

  it('answers NOT-ESTABLISHED for an unknown value rather than guessing', () => {
    expect(cicdBadgeKey('rolling-back')).toBe('unknown');
    expect(cicdBadgeKey('')).toBe('unknown');
    // And that tint is neither an accusation nor a reassurance.
    expect(OPS_BADGE[cicdBadgeKey('rolling-back')]).not.toBe(OPS_BADGE.failed);
    expect(OPS_BADGE[cicdBadgeKey('rolling-back')]).not.toBe(OPS_BADGE.ready);
    expect(OPS_BADGE[cicdBadgeKey('rolling-back')]).not.toBe(OPS_BADGE.provisioning);
  });

  it('tolerates the casing and whitespace the shell writer can produce', () => {
    // Part of the value's provenance is the buildspec's best-effort `_st()` helper.
    expect(cicdBadgeKey('Deployed')).toBe('ready');
    expect(cicdBadgeKey(' PROMOTING ')).toBe('provisioning');
  });

  it('agrees with the label helper on what is in flight', () => {
    // Both tables render color and label from these two functions over the same string, so
    // a status the label calls in-flight must not wear the terminal-success tint.
    for (const [status] of WRITTEN) {
      if (isPromotionInFlight(status)) {
        expect(cicdBadgeKey(status)).not.toBe('ready');
        expect(promotionStatusLabel(status)).toBe('Promoting to prod…');
      }
    }
  });

  it('agrees with `opsStatus` — it is the same table, not a second one', () => {
    // The anti-drift assertion, matching the label helper's. Both pages read their pill's
    // color through here, and here reads it from the single exhaustive table.
    for (const raw of ['provisioning', 'ready', 'promoting', 'deployed', 'failed', 'weird', '']) {
      expect(cicdBadgeKey(raw)).toBe(CICD_BADGE_KEY[toCicdStatus(raw)]);
    }
  });
});

// ---------------------------------------------------------------------------
// projectRoleBadge — a project role row's raw `role` string → the tint AND label its pill
// wears (E28/T10).
//
// Extracted from the ternary this had been inlined as at `ProjectAccessTab.tsx:519-523`,
// for one reason: the governance surface has THREE copies of the equivalent `roleBadge`
// helper and TWO of them label an unrecognized role **`Invoker`** — a PRIVILEGE name, and
// the worst possible kind of value to state incorrectly. A UI that answers "Invoker" for a
// role it could not identify is asserting a grant nobody made.
//
// The Ops copy happened to be correct but was unpinned, so nothing stopped it drifting the
// same way. These cases are that pin. (The governance copies are reported to their owner —
// this epic may not touch `components/governance/**`.)
// ---------------------------------------------------------------------------
describe('projectRoleBadge', () => {
  it('names and tints the three real project roles', () => {
    expect(projectRoleBadge('owner')).toEqual({ key: 'owner', label: 'Owner', cls: PROJECT_ROLE_BADGE.owner });
    expect(projectRoleBadge('maintainer')).toEqual({
      key: 'maintainer',
      label: 'Maintainer',
      cls: PROJECT_ROLE_BADGE.maintainer,
    });
    expect(projectRoleBadge('viewer')).toEqual({ key: 'viewer', label: 'Viewer', cls: PROJECT_ROLE_BADGE.viewer });
  });

  it('NEVER INVENTS a privilege name for an unknown role', () => {
    // The governance defect, refused here — and stated as "invents" rather than "says",
    // which is the distinction that makes this guard meaningful. Two governance copies
    // ANSWER `Invoker` for input they did not recognize: the name is manufactured by the
    // helper, asserting a grant nobody made. Echoing input back is a different act, so the
    // rule is that the label must be derivable from the INPUT and must never be a
    // privilege name the caller did not supply.
    for (const unknown of ['Invoker', 'invoker', 'Admin', 'admin', 'superuser', 'mystery', 'OWNER ']) {
      const badge = projectRoleBadge(unknown);
      expect(badge.key).toBe('unknown');
      // The label is the caller's own string, never a substituted one.
      expect(badge.label).toBe(unknown.trim());
      // And no rank name appears unless the caller's own value already was it.
      for (const privilege of ['Invoker', 'Owner', 'Maintainer', 'Viewer']) {
        if (unknown.trim() !== privilege) expect(badge.label).not.toBe(privilege);
      }
    }
  });

  it('does not accept a governance role name as a project role', () => {
    // `Invoker` / `Admin` are the AGENT-grant vocabulary, not project roles. Passing one in
    // must not resolve to a project rank — the two ladders are different authorities and
    // conflating them would show a standing this project's gates never granted.
    for (const foreign of ['Invoker', 'Admin']) {
      const badge = projectRoleBadge(foreign);
      expect(badge.key).toBe('unknown');
      expect(badge.cls).not.toBe(PROJECT_ROLE_BADGE.owner);
      expect(badge.cls).not.toBe(PROJECT_ROLE_BADGE.maintainer);
      expect(badge.cls).not.toBe(PROJECT_ROLE_BADGE.viewer);
    }
  });

  it('ECHOES the unrecognized role verbatim, so the fact is not hidden', () => {
    // Preserves what `ProjectAccessTab` already did (`{rowRole ? roleLabel(rowRole) : row.role}`):
    // a foreign role id is real data an operator may need to see, so it is shown as-is
    // rather than replaced. Echoing is honest; renaming it to a known role is not.
    expect(projectRoleBadge('mystery').label).toBe('mystery');
    expect(projectRoleBadge('Invoker').label).toBe('Invoker');
  });

  it('answers a blank or absent role with an explicit "Unknown"', () => {
    for (const blank of ['', '   ', null, undefined]) {
      expect(projectRoleBadge(blank).label).toBe('Unknown');
      expect(projectRoleBadge(blank).key).toBe('unknown');
    }
  });

  it('gives an unknown role a NEUTRAL tint — not a privileged one', () => {
    // The role palette is a privilege ramp (violet = most privileged). An unidentified role
    // must not borrow any rank's color, or the pill states a standing by tint alone.
    const unknown = projectRoleBadge('mystery');
    expect(unknown.cls).not.toBe(PROJECT_ROLE_BADGE.owner);
    expect(unknown.cls).not.toBe(PROJECT_ROLE_BADGE.maintainer);
    expect(unknown.cls).toMatch(/slate/);
  });

  it('is case-SENSITIVE about the real roles — the wire values are lowercase', () => {
    // `isProjectRoleName` compares exact lowercase literals, and the backend writes them.
    // A tolerant match here would let `OWNER` in through a path the role gate does not
    // recognize, so the badge would claim a standing `effectiveRole` would deny.
    expect(projectRoleBadge('OWNER').key).toBe('unknown');
  });
});

// ---------------------------------------------------------------------------
// isPromotionInFlight — the one status that means "a prod build is running for this
// row", so the row shows the in-flight treatment and offers no second Promote.
// ---------------------------------------------------------------------------
describe('isPromotionInFlight', () => {
  it('is true only for the promoting status', () => {
    expect(isPromotionInFlight('promoting')).toBe(true);
    expect(isPromotionInFlight('deployed')).toBe(false);
    expect(isPromotionInFlight('failed')).toBe(false);
    expect(isPromotionInFlight('provisioning')).toBe(false);
    expect(isPromotionInFlight('')).toBe(false);
  });
  it('tolerates the wire casing the badge mapper already tolerates', () => {
    expect(isPromotionInFlight('Promoting')).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// keepPromotionOverride — may the repos table keep its locally-polled record for a row?
//
// The override's ONLY job is to bridge the promoting window. The first cut of this rule was
// "keep it whenever it DIFFERS from the parent", which inverts the intent: differing is
// exactly when the PARENT may be the newer read, and the poller tears itself down once a
// promotion is terminal — so nothing ever refreshed the override again. The scenario below
// (`deployed` cached, a later dev build writes `failed`) is the one that shipped emerald
// "Deployed" over a red build until a full page reload.
// ---------------------------------------------------------------------------
describe('keepPromotionOverride', () => {
  it('keeps the override while it knows about a promotion the parent has not seen', () => {
    // The bridge, entering: the 202 body / a poll tick says promoting, the parent's older
    // read still says what it said before. This is the whole reason the override exists.
    expect(keepPromotionOverride('promoting', 'ready')).toBe(true);
    expect(keepPromotionOverride('promoting', 'deployed')).toBe(true);
    expect(keepPromotionOverride('promoting', 'failed')).toBe(true);
  });

  it('keeps the override while it is the terminal answer to a promotion the parent still calls in flight', () => {
    // The bridge, leaving: `promoting` can only PRECEDE deployed/failed, so a terminal
    // override over a promoting parent genuinely is the strictly later read.
    expect(keepPromotionOverride('deployed', 'promoting')).toBe(true);
    expect(keepPromotionOverride('failed', 'promoting')).toBe(true);
  });

  it('DROPS the override once the parent agrees a promotion is no longer in flight', () => {
    // THE REGRESSION. A cached `deployed` and a parent reporting `failed` differ — the old
    // predicate kept the override on exactly that, so the row showed emerald "Deployed"
    // while CI was red, with no poller left alive to correct it.
    expect(keepPromotionOverride('deployed', 'failed')).toBe(false);
    // And the mirror: a stale `failed` must not mask a parent that now reports success.
    expect(keepPromotionOverride('failed', 'deployed')).toBe(false);
    expect(keepPromotionOverride('deployed', 'ready')).toBe(false);
  });

  it('DROPS a redundant override the parent has caught up with', () => {
    expect(keepPromotionOverride('deployed', 'deployed')).toBe(false);
    expect(keepPromotionOverride('ready', 'ready')).toBe(false);
  });

  it('keeps an override only while it and the parent DISAGREE about being in flight', () => {
    // The rule, stated as the property it is — so no future edit can re-introduce
    // "differs from the parent" as a freshness proxy.
    for (const live of ['provisioning', 'ready', 'promoting', 'deployed', 'failed']) {
      for (const parent of ['provisioning', 'ready', 'promoting', 'deployed', 'failed']) {
        expect(keepPromotionOverride(live, parent)).toBe(
          isPromotionInFlight(live) !== isPromotionInFlight(parent)
        );
      }
    }
  });

  it('never outlives the parent’s data on a row the parent no longer has', () => {
    // A deleted repo must not be resurrected by an override, promoting or not.
    expect(keepPromotionOverride('promoting', undefined)).toBe(false);
    expect(keepPromotionOverride('deployed', undefined)).toBe(false);
  });

  it('agrees with the badge helpers on the casing the shell writer can produce', () => {
    expect(keepPromotionOverride('Promoting', 'ready')).toBe(true);
    expect(keepPromotionOverride(' promoting ', ' PROMOTING ')).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// promotionActionMessage — the promote route's OWN literals. It has three the other
// verbs do not (two 409s and a 502), and its 403 needs a promote-shaped remedy: neither
// "who has access" nor "to delete it" is the sentence under a Promote-to-prod button.
// Everything shared still delegates, so there is ONE mapping table.
// ---------------------------------------------------------------------------
describe('promotionActionMessage', () => {
  it('names OWNER in its 403 and says the fallback does not apply', () => {
    const out = promotionActionMessage('insufficient project role');
    expect(out).toMatch(/Owner role/i);
    expect(out).not.toMatch(/who has access/i);
    expect(out).not.toMatch(/to delete/i);
  });
  it('explains the E27A 409 no-candidate by naming the MERGE as the remedy', () => {
    // The branch an operator actually reaches: a candidate consumed by a concurrent promote
    // between this page's read and the click. It must not say "push to dev" — a dev build is
    // routine and says nothing about prod.
    const out = promotionActionMessage('no prod candidate to promote');
    expect(out).toMatch(/main/i);
    expect(out).not.toMatch(/dev branch/i);
  });
  it('keeps the two 409 preconditions DISTINCT — they have different remedies', () => {
    // Collapsing them would tell an owner on a pre-E27A row to merge to main when their
    // repository has never built at all, and vice versa.
    expect(promotionActionMessage('no prod candidate to promote')).not.toBe(
      promotionActionMessage('no dev deployment to promote')
    );
  });
  it('explains the 409 no-dev-build as a MISSING PRECONDITION, not a refusal of the caller', () => {
    const out = promotionActionMessage('no dev deployment to promote');
    expect(out).toMatch(/dev/i);
    // It must point at the remedy — ship to dev first.
    expect(out).toMatch(/before|first/i);
  });
  it('explains the 409 in-flight as already-running and retryable', () => {
    const out = promotionActionMessage('a promotion is already in flight');
    expect(out).toMatch(/already/i);
    expect(out).toMatch(/wait|finish/i);
  });
  it('explains the 502 as the build never having STARTED', () => {
    // The distinction that matters: nothing was deployed, so a retry is safe.
    const out = promotionActionMessage('failed to start the promotion build');
    expect(out).toMatch(/start/i);
    expect(out).toMatch(/again|retry/i);
  });
  it('explains the 404 rather than showing a bare “Repository not found”', () => {
    expect(promotionActionMessage('Repository not found')).toMatch(/no longer/i);
  });
  it('delegates every shared literal to the one mapping table', () => {
    expect(promotionActionMessage('could not verify project ownership')).toBe(
      roleActionMessage('could not verify project ownership')
    );
  });
  it('falls back on empty and passes an unmapped message through', () => {
    expect(promotionActionMessage('', 'Could not promote.')).toBe('Could not promote.');
    expect(promotionActionMessage('Network Error', 'f')).toBe('Network Error');
  });
  it('never surfaces one of the promote route’s raw literals', () => {
    for (const literal of [
      'insufficient project role',
      'no prod candidate to promote',
      'no dev deployment to promote',
      'a promotion is already in flight',
      'failed to start the promotion build',
      'Repository not found',
    ]) {
      expect(promotionActionMessage(literal)).not.toBe(literal);
      expect(promotionActionMessage(literal)).toMatch(/^[A-Z].*[.!]$/s);
    }
  });
});

// Every mapper on this page, swept over every backend literal at once. The N-3 defect was
// a THIRD role-gated verb whose catch mapped nothing while the other two did — so this
// pins the property ("no surface shows a raw literal") rather than three separate helpers.
describe('no message helper ever surfaces a raw backend literal', () => {
  // The literals EVERY project route can emit — so every mapper must handle all of them.
  // The promote route's three EXTRA literals are pinned against `promotionActionMessage`
  // in its own describe instead: they are emitted by exactly one route, whose only caller
  // is the promote handler, so requiring the delete/role mappers to translate them would
  // pin copy no surface can reach.
  const LITERALS = [
    'project must keep at least one owner',
    'could not verify project ownership',
    'insufficient project role',
    'invalid project role',
  ];
  const MAPPERS: ((raw: string) => string)[] = [
    (raw) => roleActionMessage(raw),
    (raw) => destructiveActionMessage(raw, 'project', 'f'),
    (raw) => destructiveActionMessage(raw, 'repository', 'f'),
    (raw) => maintainerActionMessage(raw, 'f'),
    // E27/T12 — the fourth role-gated verb. Added here, not in a helper of its own, so
    // the "no surface shows a raw literal" property covers it too.
    (raw) => promotionActionMessage(raw, 'f'),
  ];
  it('holds for every mapper × every literal', () => {
    for (const map of MAPPERS) {
      for (const literal of LITERALS) {
        expect(map(literal)).not.toBe(literal);
        // A sentence, not a fragment: capitalised and punctuated.
        expect(map(literal)).toMatch(/^[A-Z].*[.!]$/s);
      }
    }
  });
});
