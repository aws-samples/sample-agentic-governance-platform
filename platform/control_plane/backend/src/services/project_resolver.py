"""ProjectResolver: turn a validated ``Principal`` into a ``ProjectContext``.

``ProjectContext`` and ``may()`` are the enforcement primitive every project-scoped route consumes: a
route resolves the caller once, then gates each action with ``may(ctx, project_id, ProjectRole.X)``.
Routes call ``may()`` and nothing else, never ``ProjectRoleService`` directly, so a provider-derived
role source can substitute behind this seam. Project roles are checked in addition to tenant
visibility, never instead of it.

Resolution: ``role >= Role.ADMIN`` makes the context global, so ``may()`` is True regardless of the
roles map. The group source is the token's ``groups`` claim when the key is present (an empty claim is
authoritative) and otherwise a Graph ``transitiveMemberOf`` read for a caller with an ``oid``, degrading
to no groups on ``GraphError``; a dev-auth principal has neither. The match set is the caller's group
ids plus their own ``oid``, since a row may be granted to the user directly or to a group and both are
keyed by object id. The effective role per project is the maximum over the matching rows. An empty
roles map is normal and means every ``may()`` is False.

The role rows are cached for a short TTL (default 60 s) on an injectable clock. The cache has two
accepted staleness windows, both bounded by the TTL and both closed for this process by
``invalidate()``: a revoked role is still honoured until the cached rows expire, and a store fault
caches an empty list. Nothing propagates across processes, so a second task's cache keeps its snapshot.
The grant direction is not accepted, because it sits on the headline flow (a create that grants the
creator owner on one task leaves another task's snapshot pre-grant): ``refresh_project`` re-folds a
fresh single-project read on the deny path, so the cached snapshot is authoritative for granting only,
never for denying.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from types import MappingProxyType
from typing import Callable, Dict, List, Mapping, Optional, Tuple

from core.rbac import Principal, Role
from models.project_role import ROLE_NAMES, ProjectRole, ProjectRoleRecord
from services.graph_errors import GraphError
from services.project_role_service import ProjectRoleService


@dataclass(frozen=True)
class ProjectContext:
    """The caller's resolved per-project authority: ``is_global`` is True for ``Role.ADMIN``, who sees
    and may do everything; ``roles`` maps a project id to the caller's effective (maximum) role, wrapped
    read-only at the boundary so a route holding a context cannot write itself a grant."""

    is_global: bool
    roles: Mapping[str, ProjectRole]


def may(ctx: ProjectContext, project_id: Optional[str], required: ProjectRole) -> bool:
    """Whether the caller holds at least ``required`` on ``project_id``. Global admins always pass; an
    unknown project id, a ``None`` id, or a project with no matching grant is False."""
    if ctx.is_global:
        return True
    if project_id is None:
        return False
    held = ctx.roles.get(project_id)
    return held is not None and held >= required


def _fold_roles(rows: List[ProjectRoleRecord], match_set: set) -> Dict[str, ProjectRole]:
    """Fold role rows into ``project_id -> effective (maximum) role`` for one match set. The one place
    the resolution semantics live, so the cached and the fresh paths cannot drift. A row whose role
    name is unknown grants nothing."""
    roles: Dict[str, ProjectRole] = {}
    for row in rows:
        if row.principal_id not in match_set:
            continue
        role = ROLE_NAMES.get(row.role)
        if role is None:
            continue
        held = roles.get(row.project_id)
        if held is None or role > held:
            roles[row.project_id] = role
    return roles


def widen(base: ProjectContext, extra: ProjectContext) -> ProjectContext:
    """``base`` widened by ``extra``: the maximum role per project across the two. A role present in
    ``base`` is never lowered or dropped, so the result is always at least ``base``; a grant only
    ``extra`` knows about takes effect."""
    if base.is_global:
        return base
    merged: Dict[str, ProjectRole] = dict(base.roles)
    for project_id, role in extra.roles.items():
        held = merged.get(project_id)
        if held is None or role > held:
            merged[project_id] = role
    return ProjectContext(is_global=extra.is_global, roles=MappingProxyType(merged))


def context_from_rows(principal: Principal, rows: List[ProjectRoleRecord]) -> ProjectContext:
    """Fold already-read role rows into a ``ProjectContext``: pure, no I/O, no cache, the same fold
    ``resolve()`` uses. The group source is the claims only, with no Graph read, because re-awaiting
    Graph on every deny would put a network call on the 403 path; this is what lets the synchronous
    project routes share one gate with the asynchronous ones; narrower than ``resolve()``, never
    wider, which is what makes it safe as an additive second opinion on a refusal."""
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
        match_set = set(group_ids)
        if principal.oid is not None:
            match_set.add(principal.oid)

        roles = _fold_roles(self._cached_roles(), match_set)
        return ProjectContext(is_global=is_global, roles=MappingProxyType(roles))

    def refresh_project(self, principal: Principal, project_id: str) -> ProjectContext:
        """Re-resolve one project from a fresh, cache-bypassing store read: the deny path's second
        opinion, consulted only after ``may()`` on the cached context said no and ORed onto it, so it
        can only restore authority the store already says the caller holds. Scoped to one project and
        never cached. A ``ProjectRoleError`` (``ownership_unverified``) propagates; every gate treats it
        as "no extra authority" so the refusal stands."""
        rows = self._project_role_service.list_for_project_strict(project_id)
        return context_from_rows(principal, rows)

    async def _group_ids(self, principal: Principal) -> List[str]:
        if "groups" in principal.raw_claims:
            return list(principal.raw_claims.get("groups") or [])
        if principal.oid:
            try:
                return await self._graph_service.list_member_group_ids(principal.oid)
            except GraphError:
                return []
        return []

    def invalidate(self) -> None:
        """Drop the cached role rows so the next ``resolve()`` re-reads the store. Idempotent."""
        self._cache = None

    def _cached_roles(self) -> List[ProjectRoleRecord]:
        now = self._now()
        if self._cache is not None:
            cached_at, rows = self._cache
            if now - cached_at < self._ttl:
                return rows
        rows = self._project_role_service.list_all()
        self._cache = (now, rows)
        return rows
