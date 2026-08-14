"""TenantResolver — turn a validated ``Principal`` into a ``TenantContext`` (E24/T3).

The ``TenantContext`` + ``visible()`` helper are the enforcement primitive every
scoped route (Tasks 5-8) consumes: a route resolves the caller once, then gates
each record with ``visible(ctx, record.tenant_id, shared=record.shared)``.

Resolution semantics (spec §4):
  1. ``role >= Role.ADMIN`` → ``is_global=True`` immediately. Memberships are still
     resolved best-effort for display (/users/me), but a Graph failure NEVER fails
     an admin's resolve (the group-source read degrades to ``[]`` on ``GraphError``).
  2. Group source: the token's ``groups`` claim when the key is PRESENT (an empty
     claim is authoritative — Graph is NOT consulted); otherwise a Graph
     ``transitiveMemberOf`` fallback when we have the caller's ``oid``. A dev-auth
     principal (``oid=None``, ``raw_claims={}``) has neither → ``[]``. This mirrors
     ``marketplace._caller_group_ids`` (claims-first, Graph-fallback, degrade to []).
  3. Intersect the caller's group set against the TTL-cached ``tenant_service.list()``
     on ``entra_group_ids`` → the member tenant ids + records.
  4. An empty set is normal (never an error) — it yields empty lists downstream.

The tenant list is short-TTL cached (default 60s) keyed by an injectable clock so a
burst of requests does not re-scan the tenants partition per call; the clock is
injected (``now``) so tests drive cache expiry deterministically.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

from core.rbac import Principal, Role
from models.tenant import Tenant
from services.graph_service import GraphError
from services.tenant_service import TenantService


@dataclass(frozen=True)
class TenantContext:
    """The caller's resolved tenant scope — the unit every scoped route gates on."""

    is_global: bool  # True ⇔ Role.ADMIN (sees every tenant's resources)
    tenant_ids: frozenset[str]  # resolved memberships (empty for a global admin is fine)
    tenants: Tuple[Tenant, ...]  # the member Tenant records (for /users/me)


def visible(ctx: TenantContext, tenant_id: Optional[str], *, shared: bool = False) -> bool:
    """Is a resource with ``tenant_id`` (optionally ``shared``) visible to ``ctx``?

    Global admins see everything; shared resources are visible to any caller; a
    scoped caller sees only resources whose tenant is one of their memberships. An
    untagged resource (``tenant_id is None``) is invisible to non-global callers."""
    return ctx.is_global or shared or (tenant_id is not None and tenant_id in ctx.tenant_ids)


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

        matched = [
            t for t in self._cached_tenants() if group_set.intersection(t.entra_group_ids)
        ]
        return TenantContext(
            is_global=is_global,
            tenant_ids=frozenset(t.id for t in matched),
            tenants=tuple(matched),
        )

    async def resolve_oid_tenants(self, oid: str) -> frozenset[str]:
        """Resolve an ARBITRARY grant-principal oid's tenant memberships (E24/T7).

        The cross-tenant grant guard's primitive: the GRANTEE of a user→agent grant
        is identified only by an oid (no token, no claims) and may be a USER **or a
        GROUP** (the E6 Access tab grants both). Two paths, UNIONED:
          - user path — ``list_member_group_ids(oid)`` (Graph transitiveMemberOf);
            a GROUP oid 404s here (``GraphError`` → degrade to no groups, keep going);
          - direct-group path — the oid itself appearing DIRECTLY in a tenant's
            ``entra_group_ids`` (a group grantee IS the group; no Graph call needed —
            the TTL-cached tenant list already holds the linkage).
        Both intersect the same TTL-cached tenant list ``resolve`` uses. An oid
        matching neither → ``frozenset()`` — FAIL-CLOSED: an unresolvable grantee
        is treated as cross-tenant (ADMIN required), never a raise (a non-granting
        read must not 5xx the route; the guard 403s instead).
        """
        try:
            group_ids = await self._graph_service.list_member_group_ids(oid)
        except GraphError:
            group_ids = []
        # The oid itself joins the match set: a GROUP grantee's tenant linkage is
        # its oid sitting directly in ``entra_group_ids`` (E24/T7 fix — the user
        # path alone regressed same-tenant GROUP grants to ADMIN-only, since a
        # group 404s on /users/{oid}/transitiveMemberOf). An Entra oid is either
        # a user or a group, so unioning the paths cannot over-grant.
        group_set = set(group_ids) | {oid}
        return frozenset(
            t.id
            for t in self._cached_tenants()
            if group_set.intersection(t.entra_group_ids)
        )

    async def _group_ids(self, principal: Principal) -> List[str]:
        """Resolve the caller's Entra group object-ids (claims-first, Graph-fallback).

        Precedence by KEY PRESENCE: a present ``groups`` claim (even empty) is
        authoritative and Graph is not consulted. Only when the key is absent AND we
        have an ``oid`` do we fall back to Graph, degrading to ``[]`` on ``GraphError``
        (a non-granting read must never fail the resolve — spec §4). Identity comes
        ONLY from the validated principal, never a request body."""
        if "groups" in principal.raw_claims:
            return list(principal.raw_claims.get("groups") or [])
        if principal.oid:
            try:
                return await self._graph_service.list_member_group_ids(principal.oid)
            except GraphError:
                return []
        return []

    def _cached_tenants(self) -> List[Tenant]:
        """Return ``tenant_service.list()`` behind a short-TTL cache keyed by ``now``."""
        now = self._now()
        if self._cache is not None:
            cached_at, tenants = self._cache
            if now - cached_at < self._ttl:
                return tenants
        tenants = self._tenant_service.list()
        self._cache = (now, tenants)
        return tenants
