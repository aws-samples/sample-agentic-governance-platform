"""ProjectResolver — turn a validated ``Principal`` into a ``ProjectContext`` (E27/T2).

The ``ProjectContext`` + ``may()`` helper are the enforcement primitive every
project-scoped route (Tasks 3-8) consumes: a route resolves the caller once, then
gates each action with ``may(ctx, project_id, ProjectRole.X)``. Routes call ``may()``
and NOTHING else — a route must never read ``ProjectRoleService`` directly, which is
what lets a future provider-derived role source substitute in behind this seam
(design §6).

Project roles are checked **in addition** to tenant visibility, never instead of it:
``_load_visible_project`` still runs first, so a foreign-tenant project 404s before
any role logic is reached.

Resolution semantics (mirroring ``TenantResolver``):
  1. ``role >= Role.ADMIN`` → ``is_global=True`` — a global admin sees and may do
     everything, so ``may()`` short-circuits True regardless of the roles map. Grants
     are still resolved best-effort (for display), and a Graph failure NEVER fails an
     admin's resolve (the group-source read degrades to ``[]`` on ``GraphError``).
  2. Group source: the token's ``groups`` claim when the key is PRESENT (an empty
     claim is authoritative — Graph is NOT consulted); otherwise a Graph
     ``transitiveMemberOf`` fallback when we have the caller's ``oid``. A dev-auth
     principal (``oid=None``, ``raw_claims={}``) has neither → ``[]``. Copied verbatim
     from ``TenantResolver._group_ids``.
  3. Match set = the caller's group ids UNIONED with their own ``oid`` — a role row may
     be granted to the user DIRECTLY (``principal_type="user"``) or to a GROUP they
     belong to (``principal_type="group"``), and both are keyed by object id, so one
     set matches both shapes.
  4. Effective role per project = the **MAX** over all matching rows. A caller granted
     ``viewer`` directly and ``owner`` via a group is an OWNER — a group grant must
     never be capped by a weaker direct grant (or vice versa).
  5. An empty ``roles`` map is normal, never an error — it simply means the caller holds
     no project role and every ``may()`` returns False.

The role rows are short-TTL cached (default 60s) keyed by an injectable clock so a burst
of requests does not re-scan the ``project_role`` partition per call; the clock is
injected (``now``) so tests drive cache expiry deterministically — the same single-tuple
cache shape as ``TenantResolver._cached_tenants``.

**The cache has two staleness windows, both bounded by the TTL. Both are accepted:**
  - **A REVOKED role is still honoured for up to one TTL.** After ``revoke()``, a caller
    whose grant is gone keeps their old authority until the cached row list expires — so a
    just-revoked OWNER can still reach an OWNER-gated action (including the cascading
    project delete) for up to 60s. Call ``invalidate()`` after any role write to collapse
    that window to zero **for this process**. Nothing propagates across processes: a
    second ECS task's cache is untouched, so on a multi-task deployment the window still
    stands there. Accepted because role changes are rare, the tenant gate still runs
    first (a foreign-tenant project 404s before any role logic), and the caller was a
    legitimate OWNER seconds earlier.
  - **A store read failure caches an EMPTY list for up to one TTL.** Per the store's
    read-swallowing posture a transient DDB ``ClientError`` makes ``list_all()`` return
    ``[]``, and that ``[]`` is then cached like any other answer — so one blip locks a
    genuine project owner out of their own project for up to 60s even after DDB recovers.
    Fail-closed, so not a security hole, but a real availability property of this gate.
    ``invalidate()`` also clears this.

The **GRANT** direction of that first window is NOT accepted, because it is on the headline
flow rather than a rare admin edit: ``ecs_desired_count`` is 2, so a create that grants the
creator OWNER on task A leaves task B serving a pre-grant snapshot, and the operator is
refused on the project they just made (or, one gate over, silently loses the Grant / Delete /
Promote affordances) for up to a TTL. ``refresh_roles()`` closes it: a gate that DENIES on
the cached snapshot re-folds a FRESH, targeted store read for that one project before it
refuses, so a role the caller genuinely holds is never hidden by another process's cache. The
cached snapshot is therefore authoritative for GRANTING only — never for DENYING. There is
still no cross-process invalidation (deliberately: no lock service, no reconciler), so the
revoke direction keeps its documented, fail-safe window.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from types import MappingProxyType
from typing import Callable, Dict, List, Mapping, Optional, Tuple

from core.rbac import Principal, Role
from models.project_role import ROLE_NAMES, ProjectRole, ProjectRoleRecord
from services.graph_service import GraphError
from services.project_role_service import ProjectRoleService


@dataclass(frozen=True)
class ProjectContext:
    """The caller's resolved per-project authority — the unit every route gates on."""

    is_global: bool  # True ⇔ Role.ADMIN — sees and may do everything
    roles: Mapping[str, ProjectRole]  # project_id -> EFFECTIVE (max) role


def may(ctx: ProjectContext, project_id: Optional[str], required: ProjectRole) -> bool:
    """Does the caller hold at least ``required`` on ``project_id``?

    Global admins always pass. FAIL-CLOSED: an unknown project id, a ``None`` id, or a
    project with no matching grant returns False for a non-global caller."""
    if ctx.is_global:
        return True
    if project_id is None:
        return False
    held = ctx.roles.get(project_id)
    return held is not None and held >= required


def _fold_roles(
    rows: List[ProjectRoleRecord], match_set: set
) -> Dict[str, ProjectRole]:
    """Fold role rows into ``project_id -> EFFECTIVE (max) role`` for one match set.

    The ONE place the resolution semantics live, so the cached whole-partition path and the
    fresh reads below cannot drift apart (two differently-behaving folds would be a bypass of
    whichever is looser)."""
    roles: Dict[str, ProjectRole] = {}
    for row in rows:
        if row.principal_id not in match_set:
            continue
        role = ROLE_NAMES.get(row.role)
        if role is None:
            # An unrecognised wire name (a hand-edited or future row) grants NOTHING
            # rather than defaulting to some level — fail-closed, same stance as may().
            continue
        held = roles.get(row.project_id)
        if held is None or role > held:
            roles[row.project_id] = role  # effective role = MAX over matching rows
    return roles


def widen(base: ProjectContext, extra: ProjectContext) -> ProjectContext:
    """``base`` widened by ``extra`` — the MAX role per project across the two.

    The ONE way a freshly-read context is combined with the cached one, and the reason the
    fix for the process-local cache cannot narrow anything: a role present in ``base`` is
    never lowered or dropped, so the result is always ≥ ``base``. That keeps the documented
    revoke-direction window exactly as it was (a stale grant in ``base`` still stands until its
    TTL) while a grant only ``extra`` knows about takes effect immediately.

    Same max-over-sources rule ``_fold_roles`` applies within one source, for the same reason:
    a weaker grant must never cap a stronger one."""
    if base.is_global:
        return base
    merged: Dict[str, ProjectRole] = dict(base.roles)
    for project_id, role in extra.roles.items():
        held = merged.get(project_id)
        if held is None or role > held:
            merged[project_id] = role
    return ProjectContext(
        is_global=extra.is_global, roles=MappingProxyType(merged)
    )


def context_from_rows(
    principal: Principal, rows: List[ProjectRoleRecord]
) -> ProjectContext:
    """Fold ALREADY-READ role rows into a ``ProjectContext`` — pure, no I/O, no cache.

    Module-level and pure for the same reason ``may()`` is: it takes no resolver state, so a
    caller that has just read rows itself (the LIST routes read the whole partition for the §3
    governed set) folds THOSE rows at zero extra cost, and a test double does not have to grow
    a method to stay usable. Uses the same ``_fold_roles`` as ``resolve()``, so the cached and
    the fresh path cannot disagree.

    **Group source is the CLAIMS ONLY** (no Graph), unlike ``resolve()``: this is synchronous
    so the sync project routes can share one gate with the async ones, and re-awaiting Graph on
    every deny would put a network call on the 403 path. A caller whose token carries no
    ``groups`` claim therefore gets no group refresh here and keeps the old bounded window for
    a group-derived grant — narrower than ``resolve()``, never wider, which is what makes it
    safe as an ADDITIVE second opinion on a refusal."""
    match_set = set(principal.raw_claims.get("groups") or [])
    if principal.oid is not None:
        match_set.add(principal.oid)
    return ProjectContext(
        is_global=principal.role >= Role.ADMIN,
        roles=MappingProxyType(_fold_roles(rows, match_set)),
    )


class ProjectResolver:
    def __init__(
        self,
        project_role_service: ProjectRoleService,
        graph_service,
        *,
        role_list_ttl_seconds: int = 60,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._project_role_service = project_role_service
        self._graph_service = graph_service
        self._ttl = role_list_ttl_seconds
        self._now = now
        self._cache: Optional[Tuple[float, List[ProjectRoleRecord]]] = None

    async def resolve(self, principal: Principal) -> ProjectContext:
        is_global = principal.role >= Role.ADMIN

        group_ids = await self._group_ids(principal)
        # The caller's own oid joins the match set so a DIRECT user grant matches by the
        # same lookup as a group grant (a row's principal_id is an object id either way).
        match_set = set(group_ids)
        if principal.oid is not None:
            match_set.add(principal.oid)

        # Wrapped read-only at the boundary so the ``Mapping`` hint + ``frozen=True`` are a
        # guarantee the RUNTIME enforces, not just advertising: a route holding a ctx cannot
        # write itself a grant. Mirrors how ``TenantResolver`` hands back frozenset/tuple.
        roles = _fold_roles(self._cached_roles(), match_set)
        return ProjectContext(is_global=is_global, roles=MappingProxyType(roles))

    def refresh_project(self, principal: Principal, project_id: str) -> ProjectContext:
        """Re-resolve ONE project from a FRESH, cache-bypassing store read (E27 fix pass).

        The deny path's second opinion. ``resolve()``'s snapshot is authoritative for
        GRANTING but NOT for DENYING, because ``invalidate()`` is process-local: with
        ``ecs_desired_count = 2`` a grant written through task A leaves task B's snapshot
        pre-grant for up to one TTL, and the caller is then refused on a role they
        genuinely hold. Most visibly, the creator of a brand-new project is 403'd on their
        own project — the §3 ungoverned fallback cannot rescue them, because
        ``has_role_rows`` is a LIVE read that already sees the creator-OWNER row.

        A gate consults this ONLY after ``may()`` on the cached context has already said no,
        and ORs the two answers. That makes it strictly ADDITIVE: it can hand back authority
        the stale snapshot was hiding, and it can never take authority away. So the
        documented revoke-direction window is unchanged (still fail-safe, still bounded by
        the TTL) and no cross-process invalidation, lock service or reconciler is needed.

        Scoped to ONE project (a ``begins_with`` range read, not the whole partition), on the
        DENY path only, and deliberately NOT cached — it exists precisely to bypass the cache.

        ``ProjectRoleError`` (a store fault, ``ownership_unverified``) PROPAGATES: the caller
        decides, and every gate treats it as "no extra authority" so the refusal stands."""
        rows = self._project_role_service.list_for_project_strict(project_id)
        return context_from_rows(principal, rows)

    async def _group_ids(self, principal: Principal) -> List[str]:
        """Resolve the caller's Entra group object-ids (claims-first, Graph-fallback).

        Precedence by KEY PRESENCE: a present ``groups`` claim (even empty) is
        authoritative and Graph is not consulted. Only when the key is absent AND we
        have an ``oid`` do we fall back to Graph, degrading to ``[]`` on ``GraphError``
        (a non-granting read must never fail the resolve). Identity comes ONLY from the
        validated principal, never a request body."""
        if "groups" in principal.raw_claims:
            return list(principal.raw_claims.get("groups") or [])
        if principal.oid:
            try:
                return await self._graph_service.list_member_group_ids(principal.oid)
            except GraphError:
                return []
        return []

    def invalidate(self) -> None:
        """Drop the cached role rows so the next ``resolve()`` re-reads the store.

        A role write (grant/revoke) calls this so the revocation window above closes
        immediately for THIS process. Idempotent and safe on an already-empty cache."""
        self._cache = None

    def _cached_roles(self) -> List[ProjectRoleRecord]:
        """Return ``project_role_service.list_all()`` behind a short-TTL cache keyed by ``now``."""
        now = self._now()
        if self._cache is not None:
            cached_at, rows = self._cache
            if now - cached_at < self._ttl:
                return rows
        rows = self._project_role_service.list_all()
        self._cache = (now, rows)
        return rows
