import { describe, expect, expectTypeOf, it } from 'vitest';
import type { ProjectRoleRead } from '../../api/client';
import { ApiFailure, type ErrorCode } from '../../api/failure';
import { AUTH_401_HALT_MESSAGE } from '../../api/authRetry';
import { failureOf } from '../../api/loadState';
import {
  PROMOTION_REFUSAL_CODES,
  ROLE_REFUSAL_CODES,
  EMPTY_ROSTER_COPY,
  canDestroy,
  canManageRoles,
  canPromote,
  cicdBadgeKey,
  classifyPromotionFailure,
  classifyRoleFailure,
  destructiveActionMessage,
  effectiveRole,
  emptyRosterReason,
  filterNewPrincipals,
  grantRefusal,
  grantedPrincipalIds,
  isPromotionInFlight,
  keepPromotionOverride,
  promoteBlockedReason,
  isNothingToRetry,
  maintainerActionMessage,
  mayMaintainProject,
  mayOfferGrant,
  meetsRole,
  prodCandidateView,
  promotionActionMessage,
  promotionStatusLabel,
  projectRoleBadge,
  roleActionMessage,
  rosterLoadMessage,
  roleLabel,
  sortRoleRows,
  PROJECT_ROLE_BADGE,
  PROJECT_ROLE_OPTIONS,
  type PromotionRefusalKind,
  type RoleRefusalKind,
} from './projectRoles';
import { OPS_BADGE } from './opsUi';
import { CICD_BADGE_KEY, CICD_LABEL, isCicdInFlight, type CicdStatus } from './opsStatus';
import projectAccessTabRaw from './ProjectAccessTab.tsx?raw';
import { stripComments } from '../../rawSource';

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

describe('canPromote', () => {
  it('requires owner AND a pending prod candidate', () => {
    expect(canPromote('owner', 1, 'pending')).toBe(true);
    expect(canPromote('owner', 1, null)).toBe(false);
    expect(canPromote('maintainer', 1, 'pending')).toBe(false);
  });
  it('lets a platform admin promote', () => expect(canPromote(null, 2, 'pending')).toBe(true));
  it('accepts only the literal "pending": the field has one writer and the promote route clears it, so anything else is not a candidate awaiting approval', () => {
    expect(canPromote('owner', 1, 'promoted')).toBe(false);
    expect(canPromote('owner', 1, '')).toBe(false);
    expect(canPromote('owner', 1, 'Pending')).toBe(false);
  });

  it('takes no cicd_status: that field is shared by every stage, so a failed prod attempt or rollback must not hide Promote on a promotable candidate', () => {
    expect(canPromote('owner', 1, 'pending')).toBe(true);
    expect(canPromote(null, 2, 'pending')).toBe(true);
  });

  it('refuses a pending candidate whose own dev deploy failed, read from the per-tag dev_outcome_for_candidate the backend gate also reads', () => {
    expect(canPromote('owner', 1, 'pending', 'failed')).toBe(false);
    expect(canPromote(null, 2, 'pending', 'failed')).toBe(false);
  });

  it('still allows a pending candidate whose dev outcome is succeeded, started, or unknown', () => {
    expect(canPromote('owner', 1, 'pending', 'succeeded')).toBe(true);
    expect(canPromote('owner', 1, 'pending', 'started')).toBe(true);
    expect(canPromote('owner', 1, 'pending', null)).toBe(true);
    expect(canPromote('owner', 1, 'pending', undefined)).toBe(true);
  });
});

describe('prodCandidateView', () => {
  const CANDIDATE = {
    prod_candidate_status: 'pending',
    prod_candidate_sha: '3f9a1c2e8d7b6a5f4e3d2c1b0a9f8e7d6c5b4a39',
    prod_candidate_actor: 'jorge',
    prod_candidate_image_tag: 'agent-7-3f9a1c2',
  };

  it('truncates the sha to seven, matching the short sha the image tag embeds, and prefixes the GitHub login with @', () => {
    const view = prodCandidateView(CANDIDATE);
    expect(view?.shortSha).toBe('3f9a1c2');
    expect(view?.actor).toBe('@jorge');
    expect(view?.imageTag).toBe('agent-7-3f9a1c2');
  });

  it('renders nothing without a pending candidate: a gated affordance is absent, never disabled', () => {
    expect(prodCandidateView({})).toBeNull();
    expect(prodCandidateView({ prod_candidate_status: null })).toBeNull();
    expect(prodCandidateView(undefined)).toBeNull();
    expect(prodCandidateView(null)).toBeNull();
  });

  it('renders nothing for a non-pending status even with the other fields present: the promote route clears all five together, so survivors mean a partial row', () => {
    expect(prodCandidateView({ ...CANDIDATE, prod_candidate_status: 'promoted' })).toBeNull();
    expect(prodCandidateView({ ...CANDIDATE, prod_candidate_status: 'Pending' })).toBeNull();
  });

  it('agrees with canPromote about when a candidate exists, so the description and the button never disagree', () => {
    for (const status of [null, undefined, '', 'pending', 'Pending', 'promoted']) {
      expect(prodCandidateView({ ...CANDIDATE, prod_candidate_status: status }) !== null).toBe(
        canPromote('owner', 1, status ?? null)
      );
    }
  });

  it('drops a missing sha, actor or tag to null rather than rendering a bare "main @" or a lone "@"', () => {
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
  it('narrows an unknown value to null rather than guessing: fail closed, like the backend gate', () => {
    expect(effectiveRole({ effective_role: 'admin' })).toBeNull();
    expect(effectiveRole({ effective_role: '' })).toBeNull();
  });
  it('carries a group-derived owner the roster could never show: the browser cannot evaluate Entra group membership', () => {
    expect(canManageRoles(effectiveRole({ effective_role: 'owner' }), 1)).toBe(true);
  });
  it('treats the backend’s admin-as-owner report as owner, so an admin keeps their buttons with no role row anywhere', () => {
    expect(effectiveRole({ effective_role: 'owner' })).toBe('owner');
    expect(canManageRoles(effectiveRole({ effective_role: 'owner' }), 0)).toBe(true);
    expect(canPromote(effectiveRole({ effective_role: 'owner' }), 0, 'pending')).toBe(true);
  });
  it('does not hand a maintainer owner affordances: a maintainer on a project with a group grant once saw Grant and Revoke and got a 403 on click', () => {
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
  it('the role palette borrows no status tint: a role is not a health state', () => {
    for (const cls of Object.values(PROJECT_ROLE_BADGE)) {
      expect(Object.values(OPS_BADGE)).not.toContain(cls);
    }
  });
});

function row(over: Partial<ProjectRoleRead> & { principal_id: string }): ProjectRoleRead {
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
  it('reads the server hint end-to-end: a group-derived owner has no row', () => {
    expect(canDestroy(effectiveRole({ effective_role: 'owner' }), 0)).toBe(true);
    expect(canDestroy(effectiveRole({ effective_role: 'maintainer' }), 0)).toBe(false);
  });
});

describe('mayMaintainProject', () => {
  it('offers the verbs to a maintainer and an owner', () => {
    expect(mayMaintainProject('maintainer', 1, false)).toBe(true);
    expect(mayMaintainProject('owner', 1, false)).toBe(true);
  });
  it('refuses a viewer on a GOVERNED project — the route would 403', () => {
    expect(mayMaintainProject('viewer', 1, false)).toBe(false);
    expect(mayMaintainProject(null, 1, false)).toBe(false);
  });
  it('honours the ungoverned fallback: a project with no role rows admits a tenant-visible caller as maintainer, so a pre-migration project keeps its only way to add a repository', () => {
    expect(mayMaintainProject(null, 1, true)).toBe(true);
    expect(mayMaintainProject('viewer', 1, true)).toBe(true);
  });
  it('counts only a literal true as ungoverned: undefined is not established and fails closed', () => {
    expect(mayMaintainProject(null, 1, undefined)).toBe(false);
    expect(mayMaintainProject(null, 1, null)).toBe(false);
  });
  it('lets a platform admin through regardless of either input', () => {
    expect(mayMaintainProject(null, 2, false)).toBe(true);
  });
  it('is NOT the same threshold as the destructive verbs', () => {
    expect(mayMaintainProject('maintainer', 1, false)).toBe(true);
    expect(canDestroy('maintainer', 1)).toBe(false);
  });
});

describe('mayOfferGrant', () => {
  it('offers Grant to an owner once the roster loaded', () => {
    expect(mayOfferGrant('owner', 1, true)).toBe(true);
  });
  it('withholds Grant from an owner while the roster is unknown: the POST is an upsert, and over an unread roster a Viewer grant would silently downgrade an existing owner', () => {
    expect(mayOfferGrant('owner', 1, false)).toBe(false);
  });
  it('withholds Grant from a platform admin too while the roster is unknown: authority does not make the upsert safe, the missing input is the roster', () => {
    expect(mayOfferGrant(null, 2, false)).toBe(false);
    expect(mayOfferGrant(null, 2, true)).toBe(true);
  });
  it('still refuses a maintainer even with a loaded roster', () => {
    expect(mayOfferGrant('maintainer', 1, true)).toBe(false);
  });
});

describe('grantRefusal', () => {
  it('refuses a principal who already holds a role rather than upserting over it, and points at the row’s own select as the remedy', () => {
    const message = grantRefusal('g-1', new Set(['g-1']));
    expect(message).not.toBeNull();
    expect(message).toMatch(/already holds a role/i);
    expect(message).toMatch(/row/i);
  });
  it('allows a genuine ADD', () => {
    expect(grantRefusal('g-2', new Set(['g-1']))).toBeNull();
  });
  it('allows everything when the roster is empty — an empty set is only ever safe once loaded', () => {
    expect(grantRefusal('g-1', new Set())).toBeNull();
  });
});

describe('emptyRosterReason', () => {
  it('asserts ungoverned only when the SERVER says so', () => {
    expect(emptyRosterReason(true, false)).toBe('ungoverned');
  });
  it('does not assert ungoverned when the server says false: 200 with an empty list on a governed project must not claim tenant-wide maintain rights', () => {
    expect(emptyRosterReason(false, false)).toBe('unknown');
  });
  it('treats an absent bit as unknown rather than guessing', () => {
    expect(emptyRosterReason(undefined, false)).toBe('unknown');
    expect(emptyRosterReason(null, false)).toBe('unknown');
  });
  it('never infers governance from the bit for an admin: the backend derives it as not-global and no role, so an admin reads false even on an ungoverned project', () => {
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
    expect(sorted.map((r) => r.principal_id)).toEqual(['g', 'u']);
  });
  it('breaks a full tie on display name', () => {
    const sorted = sortRoleRows([
      row({ principal_id: 'b', role: 'viewer', principal_display: 'Beta' }),
      row({ principal_id: 'a', role: 'viewer', principal_display: 'Alpha' }),
    ]);
    expect(sorted.map((r) => r.principal_id)).toEqual(['a', 'b']);
  });
  it('sorts by principal id when a display name is blank, so a row whose name never resolved still sorts deterministically', () => {
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
    const rows = [row({ principal_id: 'held' })];
    const hits = [{ id: 'held' }, { id: 'fresh' }];
    expect(filterNewPrincipals(hits, grantedPrincipalIds(rows)).map((h) => h.id)).toEqual(['fresh']);
  });
});

function apiFailure({ status, code, detail = '', message = detail }: { status: number; code: ErrorCode | null; detail?: string; message?: string }): ApiFailure {
  return new ApiFailure(status, code, detail, message);
}

const ROLE_REFUSAL: Record<RoleRefusalKind, ApiFailure> = {
  last_owner: apiFailure({ status: 409, code: 'project_role.last_owner', detail: 'project must keep at least one owner' }),
  ownership_unverified: apiFailure({
    status: 503,
    code: 'project_role.ownership_unverified',
    detail: 'could not verify project ownership',
  }),
  insufficient: apiFailure({ status: 403, code: 'project_role.insufficient', detail: 'insufficient project role' }),
  validation: apiFailure({ status: 400, code: 'project_role.validation', detail: 'invalid project role' }),
};

describe('ROLE_REFUSAL_CODES — typed on the generated ErrorCode', () => {
  it('satisfies Record<RoleRefusalKind, readonly ErrorCode[]>, so a renamed backend code fails tsc where this table names it', () => {
    expectTypeOf(ROLE_REFUSAL_CODES).toExtend<Record<RoleRefusalKind, readonly ErrorCode[]>>();
  });

  it('names at least one code per kind and no code under two kinds', () => {
    const all = Object.values(ROLE_REFUSAL_CODES).flat();
    for (const codes of Object.values(ROLE_REFUSAL_CODES)) expect(codes.length).toBeGreaterThan(0);
    expect(new Set(all).size).toBe(all.length);
  });

  it('classifies by code alone: the sentence under no code is unrecognised, a reworded detail under the code is not', () => {
    expect(classifyRoleFailure(apiFailure({ status: 403, code: null, detail: 'insufficient project role' }))).toBeNull();
    expect(
      classifyRoleFailure(apiFailure({ status: 403, code: 'project_role.insufficient', detail: 'Insufficient Project Role' })),
    ).toBe('insufficient');
    expect(classifyRoleFailure(apiFailure({ status: 404, code: 'project.not_found', detail: 'Project not found' }))).toBeNull();
  });
});

describe('roleActionMessage', () => {
  it('explains the 409 last-owner refusal AND the remedy', () => {
    const out = roleActionMessage(ROLE_REFUSAL.last_owner);
    expect(out).toMatch(/must keep at least one owner/i);
    expect(out).toMatch(/grant owner to someone else/i);
  });
  it('explains the 503 as REFUSED, not failed, and as retryable', () => {
    const out = roleActionMessage(ROLE_REFUSAL.ownership_unverified);
    expect(out).toMatch(/refused/i);
    expect(out).toMatch(/try again/i);
  });
  it('turns the 403 into a sentence naming the role needed', () => {
    expect(roleActionMessage(ROLE_REFUSAL.insufficient)).toMatch(/need the Owner role/i);
  });
  it('turns the 400 into a sentence', () => {
    expect(roleActionMessage(ROLE_REFUSAL.validation)).toMatch(/isn’t valid for this project/i);
  });
  it('never returns a raw mapped detail', () => {
    for (const failure of Object.values(ROLE_REFUSAL)) {
      expect(roleActionMessage(failure), failure.message).not.toBe(failure.message);
    }
  });
  it('keys on the code, so a reworded detail still maps and the old sentence under no code is shown as sent', () => {
    expect(
      roleActionMessage(apiFailure({ status: 403, code: 'project_role.insufficient', detail: 'Insufficient Project Role' })),
    ).toMatch(/need the Owner role/i);
    expect(roleActionMessage(apiFailure({ status: 403, code: null, detail: 'insufficient project role' }), 'Could not load.')).toBe(
      'insufficient project role',
    );
  });
  it('returns the fallback only for a rejection with no message', () => {
    expect(roleActionMessage(new Error(''), 'Could not load.')).toBe('Could not load.');
    expect(roleActionMessage(new Error('   '), 'Could not load.')).toBe('Could not load.');
    expect(roleActionMessage(null, 'Could not load.')).toBe('Could not load.');
  });
  it('passes an unmapped message through rather than swallowing it', () => {
    expect(roleActionMessage(new Error('Network Error'), 'Could not load.')).toBe('Network Error');
    expect(roleActionMessage(apiFailure({ status: 404, code: 'project.not_found', detail: 'Project not found' }), 'Could not load.')).toBe(
      'Project not found',
    );
  });
});

describe('destructiveActionMessage', () => {
  it('names deleting the PROJECT in its 403, not role management', () => {
    const out = destructiveActionMessage(ROLE_REFUSAL.insufficient, 'project', 'fallback');
    expect(out).toMatch(/Owner role on this project to delete it/i);
    expect(out).not.toMatch(/who has access/i);
  });
  it('names the repository teardown in its 403 and says what is destroyed, since the cascade is irreversible', () => {
    const out = destructiveActionMessage(ROLE_REFUSAL.insufficient, 'repository', 'fallback');
    expect(out).toMatch(/delete a repository/i);
    expect(out).toMatch(/runtime/i);
  });
  it('delegates every other refusal to the one mapping table', () => {
    expect(destructiveActionMessage(ROLE_REFUSAL.last_owner, 'project', 'f')).toBe(
      roleActionMessage(ROLE_REFUSAL.last_owner)
    );
  });
  it('falls back on empty and passes an unmapped message through', () => {
    expect(destructiveActionMessage(new Error(''), 'project', 'Failed to delete the project.')).toBe(
      'Failed to delete the project.'
    );
    expect(destructiveActionMessage(new Error('Network Error'), 'project', 'f')).toBe('Network Error');
  });
  it('never surfaces the raw 403 detail for either subject', () => {
    for (const subject of ['project', 'repository'] as const) {
      expect(destructiveActionMessage(ROLE_REFUSAL.insufficient, subject, 'f')).not.toBe(
        ROLE_REFUSAL.insufficient.message
      );
    }
  });
});

describe('maintainerActionMessage', () => {
  it('names the MAINTAINER threshold in its 403, not Owner', () => {
    const out = maintainerActionMessage(ROLE_REFUSAL.insufficient, 'f');
    expect(out).toMatch(/Maintainer role/i);
    expect(out).not.toMatch(/who has access/i);
    expect(out).not.toMatch(/to delete/i);
  });
  it('never surfaces the raw 403 detail', () => {
    expect(maintainerActionMessage(ROLE_REFUSAL.insufficient, 'f')).not.toBe(ROLE_REFUSAL.insufficient.message);
    expect(
      maintainerActionMessage(apiFailure({ status: 403, code: 'project_role.insufficient', detail: 'Insufficient Project Role' }), 'f'),
    ).toMatch(/Maintainer role/i);
  });
  it('delegates every other refusal to the one mapping table', () => {
    expect(maintainerActionMessage(ROLE_REFUSAL.ownership_unverified, 'f')).toBe(
      roleActionMessage(ROLE_REFUSAL.ownership_unverified)
    );
  });
  it('falls back on empty and passes an unmapped message through', () => {
    expect(maintainerActionMessage(new Error(''), 'Failed to create the repository.')).toBe(
      'Failed to create the repository.'
    );
    expect(maintainerActionMessage(new Error('Network Error'), 'f')).toBe('Network Error');
  });
});

describe('isNothingToRetry — the retry route’s 409 for a repository that already completed', () => {
  it('recognises project.nothing_to_retry whatever the detail says', () => {
    expect(isNothingToRetry(apiFailure({ status: 409, code: 'project.nothing_to_retry', detail: 'Nothing to retry' }))).toBe(true);
    expect(isNothingToRetry(apiFailure({ status: 409, code: 'project.nothing_to_retry', detail: 'reworded' }))).toBe(true);
  });
  it('does not read the sentence, and is false for any other refusal or rejection', () => {
    expect(isNothingToRetry(apiFailure({ status: 409, code: null, detail: 'Nothing to retry' }))).toBe(false);
    expect(isNothingToRetry(new Error('Nothing to retry'))).toBe(false);
    expect(isNothingToRetry(ROLE_REFUSAL.insufficient)).toBe(false);
    expect(isNothingToRetry(null)).toBe(false);
  });
});

describe('rosterLoadMessage — what the access tab says when the roster read fails', () => {
  it('tells a principal with no role on the project that the list is hidden, not that the load failed', () => {
    expect(rosterLoadMessage(ROLE_REFUSAL.insufficient)).toBe(
      'You don’t have a role on this project, so its access list is hidden. Ask an owner to grant you one.'
    );
  });
  it('hands every other refusal to roleActionMessage with the load fallback', () => {
    expect(rosterLoadMessage(ROLE_REFUSAL.ownership_unverified)).toBe(roleActionMessage(ROLE_REFUSAL.ownership_unverified));
    expect(rosterLoadMessage(new Error('Network Error'))).toBe('Network Error');
    expect(rosterLoadMessage(null)).toBe('Failed to load the access list.');
  });
  it('shows the interceptor’s 401 copy as sent when the load state carries its codeless 401 failure', () => {
    const state = failureOf(new ApiFailure(401, null, undefined, AUTH_401_HALT_MESSAGE), 'Failed to load the access list.');
    expect(state.failure).toMatchObject({ status: 401, code: null, message: AUTH_401_HALT_MESSAGE });
    expect(rosterLoadMessage(state.failure ?? state.message)).toBe(AUTH_401_HALT_MESSAGE);
    expect(stripComments(projectAccessTabRaw)).toContain('rosterLoadMessage(rosterState.failure ?? rosterState.message)');
  });
});

describe('promoteBlockedReason', () => {
  it('agrees with canPromote on the allow case', () => {
    expect(promoteBlockedReason('owner', 1, 'pending')).toBe('ok');
    expect(canPromote('owner', 1, 'pending')).toBe(true);
  });
  it('separates “not yet” from “not you”: an owner with no candidate gets the merge-to-main sentence, a maintainer with a candidate gets nothing', () => {
    expect(promoteBlockedReason('owner', 1, null)).toBe('no-candidate');
    expect(promoteBlockedReason('maintainer', 1, 'pending')).toBe('not-owner');
  });
  it('reports the role refusal first when both apply, so a viewer is not told to merge to main', () => {
    expect(promoteBlockedReason('viewer', 1, null)).toBe('not-owner');
    expect(promoteBlockedReason(null, 0, null)).toBe('not-owner');
  });
  it('lets a platform admin through, matching canPromote', () => {
    expect(promoteBlockedReason(null, 2, 'pending')).toBe('ok');
    expect(promoteBlockedReason(null, 2, null)).toBe('no-candidate');
  });
  it('treats an absent status field the same as a null one', () => {
    expect(promoteBlockedReason('owner', 1, undefined)).toBe('no-candidate');
    expect(promoteBlockedReason('owner', 1, '')).toBe('no-candidate');
  });
  it('does not treat a dev image tag as a candidate: a repo whose main has not moved has nothing to approve, and the route would answer 409', () => {
    expect(promoteBlockedReason('owner', 1, 'a-1-abc')).toBe('no-candidate');
  });
  it('never says ok where canPromote says false, for every combination: ok is exactly canPromote true, never a truthiness check', () => {
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

  it('still refuses nothing extra when no dev-outcome fact is given', () => {
    expect(promoteBlockedReason('owner', 1, 'pending')).toBe('ok');
    expect(promoteBlockedReason('maintainer', 1, 'pending')).toBe('not-owner');
    expect(canPromote('owner', 1, 'pending')).toBe(true);
  });

  describe("'dev-deploy-failed', reinstated on the backend's own per-tag fact", () => {
    it('answers dev-deploy-failed for a pending candidate whose own dev deploy failed', () => {
      expect(promoteBlockedReason('owner', 1, 'pending', 'failed')).toBe('dev-deploy-failed');
    });
    it('agrees with canPromote: never ok when the dev outcome is failed', () => {
      expect(canPromote('owner', 1, 'pending', 'failed')).toBe(false);
    });
    it('does not fire without a pending candidate — role/no-candidate still win', () => {
      expect(promoteBlockedReason('owner', 1, null, 'failed')).toBe('no-candidate');
      expect(promoteBlockedReason('maintainer', 1, 'pending', 'failed')).toBe('not-owner');
    });
    it('a succeeded/started/unknown dev outcome still reads ok', () => {
      for (const outcome of ['succeeded', 'started', null, undefined]) {
        expect(promoteBlockedReason('owner', 1, 'pending', outcome)).toBe('ok');
      }
    });
  });
});

describe('promotionStatusLabel', () => {
  it('names the promoting state', () => expect(promotionStatusLabel('promoting')).toBe('Deploying…'));
  it('maps terminal states, with failed reading "Delivery failed" since the runtime pill beside it has its own failed', () => {
    expect(promotionStatusLabel('deployed')).toBe('Deployed');
    expect(promotionStatusLabel('failed')).toBe('Delivery failed');
  });

  it('labels the materialize statuses, with ready reading "Not built yet": scaffolded, nothing built or deployed', () => {
    expect(promotionStatusLabel('provisioning')).toBe('Provisioning');
    expect(promotionStatusLabel('ready')).toBe('Not built yet');
  });

  it('never leaks the raw lowercase wire value for a known status: every label is sentence case', () => {
    const written: CicdStatus[] = ['promoting', 'deployed', 'failed', 'provisioning', 'ready'];
    for (const status of written) {
      expect(promotionStatusLabel(status)).not.toBe(status);
      expect(promotionStatusLabel(status)).toMatch(/^[A-Z]/);
    }
  });

  it('agrees with `opsStatus` — it is the same table, not a second one', () => {
    const members: CicdStatus[] = ['promoting', 'deployed', 'failed', 'provisioning', 'ready', 'unknown'];
    for (const status of members) {
      expect(promotionStatusLabel(status)).toBe(CICD_LABEL[status]);
    }
  });
});

describe('cicdBadgeKey', () => {
  const WRITTEN: [CicdStatus, keyof typeof OPS_BADGE][] = [
    ['provisioning', 'provisioning'],
    ['ready', 'pending'],
    ['promoting', 'provisioning'],
    ['deployed', 'ready'],
    ['failed', 'failed'],
  ];

  it('maps every status a backend writer produces to its tint, with ready amber rather than emerald', () => {
    for (const [status, key] of WRITTEN) {
      expect(cicdBadgeKey(status)).toBe(key);
    }
  });

  it('never lets a real status wear the unknown tint: the fall-through is slate, not provisioning’s amber', () => {
    for (const [status] of WRITTEN) {
      expect(cicdBadgeKey(status)).not.toBe('unknown');
    }
  });

  it('gives deployed the terminal-success tint and promoting the in-flight one, so a repo live in production never shares a tint with one being promoted', () => {
    expect(cicdBadgeKey('deployed')).toBe('ready');
    expect(cicdBadgeKey('promoting')).toBe('provisioning');
    expect(OPS_BADGE[cicdBadgeKey('deployed')]).not.toBe(OPS_BADGE[cicdBadgeKey('promoting')]);
    expect(OPS_BADGE[cicdBadgeKey('promoting')]).toBe(OPS_BADGE[cicdBadgeKey('provisioning')]);
  });

  it('agrees with the label helper on what is in flight', () => {
    for (const [status] of WRITTEN) {
      if (isPromotionInFlight(status)) {
        expect(cicdBadgeKey(status)).not.toBe('ready');
        expect(promotionStatusLabel(status)).toBe('Deploying…');
      }
    }
  });

  it('agrees with `opsStatus` — it is the same table, not a second one', () => {
    const members: CicdStatus[] = ['provisioning', 'ready', 'promoting', 'deployed', 'failed', 'unknown'];
    for (const status of members) {
      expect(cicdBadgeKey(status)).toBe(CICD_BADGE_KEY[status]);
    }
  });
});

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

  it('never invents a privilege name for an unknown role: the label is the caller’s own string, never a rank it did not supply', () => {
    for (const unknown of ['Invoker', 'invoker', 'Admin', 'admin', 'superuser', 'mystery', 'OWNER ']) {
      const badge = projectRoleBadge(unknown);
      expect(badge.key).toBe('unknown');
      expect(badge.label).toBe(unknown.trim());
      for (const privilege of ['Invoker', 'Owner', 'Maintainer', 'Viewer']) {
        if (unknown.trim() !== privilege) expect(badge.label).not.toBe(privilege);
      }
    }
  });

  it('does not accept a governance role name as a project role: Invoker and Admin are the agent-grant vocabulary', () => {
    for (const foreign of ['Invoker', 'Admin']) {
      const badge = projectRoleBadge(foreign);
      expect(badge.key).toBe('unknown');
      expect(badge.cls).not.toBe(PROJECT_ROLE_BADGE.owner);
      expect(badge.cls).not.toBe(PROJECT_ROLE_BADGE.maintainer);
      expect(badge.cls).not.toBe(PROJECT_ROLE_BADGE.viewer);
    }
  });

  it('echoes the unrecognised role verbatim, so a foreign role id stays visible', () => {
    expect(projectRoleBadge('mystery').label).toBe('mystery');
    expect(projectRoleBadge('Invoker').label).toBe('Invoker');
  });

  it('answers a blank or absent role with an explicit "Unknown"', () => {
    for (const blank of ['', '   ', null, undefined]) {
      expect(projectRoleBadge(blank).label).toBe('Unknown');
      expect(projectRoleBadge(blank).key).toBe('unknown');
    }
  });

  it('gives an unknown role a neutral slate that is none of the three rank tints', () => {
    const unknown = projectRoleBadge('mystery');
    expect(unknown.cls).not.toBe(PROJECT_ROLE_BADGE.owner);
    expect(unknown.cls).not.toBe(PROJECT_ROLE_BADGE.maintainer);
    expect(unknown.cls).not.toBe(PROJECT_ROLE_BADGE.viewer);
    expect(unknown.cls).toMatch(/slate/);
  });

  it('is case-sensitive about the real roles: OWNER is unknown, since the role gates compare exact lowercase literals', () => {
    expect(projectRoleBadge('OWNER').key).toBe('unknown');
  });
});

describe('isPromotionInFlight', () => {
  it('is true only for promoting: a prod promotion, not any in-flight delivery', () => {
    expect(isPromotionInFlight('promoting')).toBe(true);
    expect(isPromotionInFlight('deployed')).toBe(false);
    expect(isPromotionInFlight('failed')).toBe(false);
    expect(isPromotionInFlight('provisioning')).toBe(false);
  });
  it('is narrower than opsStatus.isCicdInFlight: provisioning is in flight for delivery but is not a promotion', () => {
    expect(isCicdInFlight('provisioning')).toBe(true);
    expect(isPromotionInFlight('provisioning')).toBe(false);
    for (const status of ['provisioning', 'ready', 'promoting', 'deployed', 'failed', 'unknown'] as const) {
      if (isPromotionInFlight(status)) expect(isCicdInFlight(status)).toBe(true);
    }
  });
});

describe('keepPromotionOverride', () => {
  it('keeps the override while it knows about a promotion the parent has not seen', () => {
    expect(keepPromotionOverride('promoting', 'ready')).toBe(true);
    expect(keepPromotionOverride('promoting', 'deployed')).toBe(true);
    expect(keepPromotionOverride('promoting', 'failed')).toBe(true);
  });

  it('keeps the override while it is the terminal answer to a promotion the parent still calls in flight', () => {
    expect(keepPromotionOverride('deployed', 'promoting')).toBe(true);
    expect(keepPromotionOverride('failed', 'promoting')).toBe(true);
  });

  it('drops the override once the parent agrees no promotion is in flight: a cached deployed over a parent reporting failed must not show emerald over a red build', () => {
    expect(keepPromotionOverride('deployed', 'failed')).toBe(false);
    expect(keepPromotionOverride('failed', 'deployed')).toBe(false);
    expect(keepPromotionOverride('deployed', 'ready')).toBe(false);
  });

  it('drops a redundant override the parent has caught up with', () => {
    expect(keepPromotionOverride('deployed', 'deployed')).toBe(false);
    expect(keepPromotionOverride('ready', 'ready')).toBe(false);
  });

  it('keeps an override only while it and the parent disagree about being in flight, never because the statuses merely differ', () => {
    const members: CicdStatus[] = ['provisioning', 'ready', 'promoting', 'deployed', 'failed'];
    for (const live of members) {
      for (const parent of members) {
        expect(keepPromotionOverride(live, parent)).toBe(
          isPromotionInFlight(live) !== isPromotionInFlight(parent)
        );
      }
    }
  });

  it('never outlives the parent’s data on a row the parent no longer has', () => {
    expect(keepPromotionOverride('promoting', undefined)).toBe(false);
    expect(keepPromotionOverride('deployed', undefined)).toBe(false);
  });
});

const PROMOTION_REFUSAL: Record<PromotionRefusalKind, ApiFailure> = {
  insufficient: ROLE_REFUSAL.insufficient,
  no_prod_candidate: apiFailure({ status: 409, code: 'project.no_prod_candidate', detail: 'no prod candidate to promote' }),
  no_dev_build: apiFailure({ status: 409, code: 'project.no_dev_build', detail: 'no dev deployment to promote' }),
  eval_gate: apiFailure({
    status: 409,
    code: 'project.eval_gate',
    detail: 'Prod promotion needs a passing evaluation for this build (or an override with a reason).',
  }),
  promote_in_flight: apiFailure({ status: 409, code: 'project.promote_in_flight', detail: 'a promotion is already in flight' }),
  promote_failed: apiFailure({ status: 502, code: 'project.promote_failed', detail: 'failed to start the promotion build' }),
  repo_not_found: apiFailure({ status: 404, code: 'project.repo_not_found', detail: 'Repository not found' }),
};

describe('PROMOTION_REFUSAL_CODES — typed on the generated ErrorCode', () => {
  it('satisfies Record<PromotionRefusalKind, readonly ErrorCode[]>, so a renamed backend code fails tsc where this table names it', () => {
    expectTypeOf(PROMOTION_REFUSAL_CODES).toExtend<Record<PromotionRefusalKind, readonly ErrorCode[]>>();
  });

  it('names at least one code per kind and no code under two kinds', () => {
    const all = Object.values(PROMOTION_REFUSAL_CODES).flat();
    for (const codes of Object.values(PROMOTION_REFUSAL_CODES)) expect(codes.length).toBeGreaterThan(0);
    expect(new Set(all).size).toBe(all.length);
  });

  it('classifies by code alone: the detail under no code is unrecognised, a reworded detail under the code is not', () => {
    expect(classifyPromotionFailure(apiFailure({ status: 409, code: null, detail: 'a promotion is already in flight' }))).toBeNull();
    expect(classifyPromotionFailure(apiFailure({ status: 409, code: 'project.eval_gate', detail: 'reworded' }))).toBe('eval_gate');
  });
});

describe('promotionActionMessage', () => {
  it('names Owner in its 403 and says the ungoverned fallback does not apply to shipping to prod', () => {
    const out = promotionActionMessage(PROMOTION_REFUSAL.insufficient);
    expect(out).toMatch(/Owner role/i);
    expect(out).not.toMatch(/who has access/i);
    expect(out).not.toMatch(/to delete/i);
  });
  it('explains the 409 no-candidate by naming the merge to main as the remedy, never a push to dev', () => {
    const out = promotionActionMessage(PROMOTION_REFUSAL.no_prod_candidate);
    expect(out).toMatch(/main/i);
    expect(out).not.toMatch(/dev branch/i);
  });
  it('keeps the two 409 preconditions distinct: merge to main and ship to dev are different remedies', () => {
    expect(promotionActionMessage(PROMOTION_REFUSAL.no_prod_candidate)).not.toBe(
      promotionActionMessage(PROMOTION_REFUSAL.no_dev_build)
    );
  });
  it('explains the 409 no-dev-build as a missing precondition, not a refusal of the caller', () => {
    const out = promotionActionMessage(PROMOTION_REFUSAL.no_dev_build);
    expect(out).toMatch(/dev/i);
    expect(out).toMatch(/before|first/i);
  });
  it('explains the 409 in-flight as already-running and retryable', () => {
    const out = promotionActionMessage(PROMOTION_REFUSAL.promote_in_flight);
    expect(out).toMatch(/already/i);
    expect(out).toMatch(/wait|finish/i);
  });
  it('explains the 502 as the build never having started, so nothing reached prod and a retry is safe', () => {
    const out = promotionActionMessage(PROMOTION_REFUSAL.promote_failed);
    expect(out).toMatch(/start/i);
    expect(out).toMatch(/again|retry/i);
  });
  it('explains the 404 rather than showing a bare “Repository not found”', () => {
    expect(promotionActionMessage(PROMOTION_REFUSAL.repo_not_found)).toMatch(/no longer/i);
  });
  it('maps the 409 eval gate to the override remedy, not a role refusal', () => {
    const out = promotionActionMessage(PROMOTION_REFUSAL.eval_gate);
    expect(out).toMatch(/override/i);
    expect(out).toMatch(/reason/i);
    expect(out).not.toMatch(/Owner role/i);
  });
  it('delegates every shared refusal to the one mapping table', () => {
    expect(promotionActionMessage(ROLE_REFUSAL.ownership_unverified)).toBe(
      roleActionMessage(ROLE_REFUSAL.ownership_unverified)
    );
  });
  it('falls back on empty and passes an unmapped message through as sent', () => {
    expect(promotionActionMessage(new Error(''), 'Could not promote.')).toBe('Could not promote.');
    expect(promotionActionMessage(new Error('Network Error'), 'f')).toBe('Network Error');
    expect(promotionActionMessage(apiFailure({ status: 409, code: null, detail: 'a promotion is already in flight' }), 'f')).toBe(
      'a promotion is already in flight'
    );
  });
  it('never surfaces one of the promote route’s raw details', () => {
    for (const failure of Object.values(PROMOTION_REFUSAL)) {
      expect(promotionActionMessage(failure), failure.message).not.toBe(failure.message);
      expect(promotionActionMessage(failure), failure.message).toMatch(/^[A-Z].*[.!]$/s);
    }
  });
});

describe('no message helper ever surfaces a raw backend detail', () => {
  const MAPPERS: ((failure: ApiFailure) => string)[] = [
    (failure) => roleActionMessage(failure),
    (failure) => destructiveActionMessage(failure, 'project', 'f'),
    (failure) => destructiveActionMessage(failure, 'repository', 'f'),
    (failure) => maintainerActionMessage(failure, 'f'),
    (failure) => promotionActionMessage(failure, 'f'),
  ];
  it('holds for every mapper and every role refusal, as a capitalised, punctuated sentence', () => {
    for (const map of MAPPERS) {
      for (const failure of Object.values(ROLE_REFUSAL)) {
        expect(map(failure), failure.message).not.toBe(failure.message);
        expect(map(failure), failure.message).toMatch(/^[A-Z].*[.!]$/s);
      }
    }
  });
});

describe('ProjectAccessTab wires the roster to the grant picker', () => {
  it('the roster the grant picker excludes against is the carried read while a refetch is in flight, and empty only after a failed read', () => {
    const src = stripComments(projectAccessTabRaw);
    expect(src).toContain(
      "const rows = rosterState.kind === 'error' ? EMPTY_ROSTER : (rosterState.data ?? EMPTY_ROSTER);",
    );
    expect(src).toContain('const existingIds = useMemo(() => grantedPrincipalIds(rows), [rows]);');
  });

  it('classifies the roster read failure through the model, never by its sentence, and lets the failure reach useLoad unwrapped', () => {
    const src = stripComments(projectAccessTabRaw);
    expect(src).toContain('rosterLoadMessage(');
    expect(src).not.toMatch(/insufficient project role/);
    expect(src).not.toMatch(/throw new Error\(/);
    expect(src).not.toMatch(/roleActionMessage\(err instanceof Error/);
  });
});
