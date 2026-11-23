"""Turn a validated ``Principal`` into a ``TenantContext``, the scope every tenant-gated route
checks records against with :func:`visible`.

Resolution: a role at or above ``Role.ADMIN`` is global. The caller's group ids come from the
token's ``groups`` claim when the key is present (an empty claim is authoritative), else from a
Graph ``transitiveMemberOf`` read when the principal has an ``oid``, degrading to ``[]`` on
``GraphError``; a dev-auth principal has neither. The group set is intersected with the tenant
list on ``entra_group_ids``; an empty result is normal. The tenant list is cached for
``tenant_list_ttl_seconds`` behind an injectable clock.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

from core.rbac import Principal, Role
from models.tenant import Tenant
from services.graph_errors import GraphError
from services.tenant_service import TenantError, TenantService


@dataclass(frozen=True)
class TenantContext:
    """The caller's resolved tenant scope: ``is_global`` for an admin who sees every tenant,
    the member tenant ids, and the member records for ``/users/me``."""

    is_global: bool
    tenant_ids: frozenset[str]
    tenants: Tuple[Tenant, ...]


def visible(ctx: TenantContext, tenant_id: Optional[str], *, shared: bool = False) -> bool:
    """Global admins see everything; shared resources are visible to any caller; a scoped caller
    sees only resources whose tenant is one of their memberships. An untagged resource
    (``tenant_id is None``) is invisible to non-global callers."""
    return ctx.is_global or shared or (tenant_id is not None and tenant_id in ctx.tenant_ids)


def require_member(ctx: TenantContext, tenant_id: str) -> None:
    """The refusal is ``TenantError`` kind ``not_permitted``: no resource exists yet, so it is a 403,
    never the 404 a foreign record answers."""
    if not ctx.is_global and tenant_id not in ctx.tenant_ids:
        raise TenantError(f"caller is not a member of tenant {tenant_id}", kind="not_permitted")


class TenantResolver:
    def __init__(
        self,
        tenant_service: TenantService,
        graph_service,
        *,
        tenant_list_ttl_seconds: int = 60,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._tenant_service = tenant_service
        self._graph_service = graph_service
        self._ttl = tenant_list_ttl_seconds
        self._now = now
        self._cache: Optional[Tuple[float, List[Tenant]]] = None

    async def resolve(self, principal: Principal) -> TenantContext:
        is_global = principal.role >= Role.ADMIN

        group_ids = await self._group_ids(principal)
        group_set = set(group_ids)

        matched = [t for t in self._cached_tenants() if group_set.intersection(t.entra_group_ids)]
        return TenantContext(
            is_global=is_global,
            tenant_ids=frozenset(t.id for t in matched),
            tenants=tuple(matched),
        )

    async def resolve_oid_tenants(self, oid: str) -> frozenset[str]:
        """The tenant memberships of an arbitrary grantee oid, which may be a user or a group.

        A user's groups come from Graph (a group oid 404s there and degrades to none); the oid
        itself joins the match set, since a group grantee's linkage is its oid sitting directly
        in a tenant's ``entra_group_ids``. An oid matching nothing resolves to the empty set, so
        an unresolvable grantee is treated as cross-tenant rather than raising.
        """
        try:
            group_ids = await self._graph_service.list_member_group_ids(oid)
        except GraphError:
            group_ids = []
        group_set = set(group_ids) | {oid}
        return frozenset(t.id for t in self._cached_tenants() if group_set.intersection(t.entra_group_ids))

    async def _group_ids(self, principal: Principal) -> List[str]:
        if "groups" in principal.raw_claims:
            return list(principal.raw_claims.get("groups") or [])
        if principal.oid:
            try:
                return await self._graph_service.list_member_group_ids(principal.oid)
            except GraphError:
                return []
        return []

    def _cached_tenants(self) -> List[Tenant]:
        now = self._now()
        if self._cache is not None:
            cached_at, tenants = self._cache
            if now - cached_at < self._ttl:
                return tenants
        tenants = self._tenant_service.list()
        self._cache = (now, tenants)
        return tenants
