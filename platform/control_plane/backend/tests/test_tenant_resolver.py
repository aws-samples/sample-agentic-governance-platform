"""Resolver tests for the E24 TenantResolver (multi-tenancy, Task 3).

The resolver turns a validated ``Principal`` into a ``TenantContext`` — the
enforcement primitive every scoped route (Tasks 5-8) consumes via ``visible()``.

These tests ARE the resolver spec (task-3-brief §Step 1). They inject fakes:
a ``MagicMock`` tenant_service whose ``.list()`` returns REAL ``Tenant`` records
(so the group-intersection is exercised) + an ``AsyncMock`` graph_service (so NO
live Graph is touched). The repo is NOT in pytest-asyncio ``auto`` mode (no
pytest.ini / pyproject config), so every async test is decorated
``@pytest.mark.asyncio`` explicitly (same as ``test_graph_service.py`` /
``test_governance_graph_service.py``).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from core.rbac import Principal, Role
from models.tenant import Tenant, TenantStageConfig
from services.graph_service import GraphError
from services.tenant_resolver import TenantContext, TenantResolver, visible

FIXED_TS = "2026-07-13T00:00:00+00:00"


# --- builders ---------------------------------------------------------------


def _tenant(*, id: str, name: str, group_ids: list[str]) -> Tenant:
    return Tenant(
        id=id,
        name=name,
        line_of_business="LoB",
        entra_group_ids=group_ids,
        stages={
            "dev": TenantStageConfig(account_id="111111111111"),
            "prod": TenantStageConfig(account_id="222222222222"),
        },
        description="",
        created_by="seed",
        created_at=FIXED_TS,
        updated_at=FIXED_TS,
    )


def _principal(*, role: Role, oid=None, raw_claims=None) -> Principal:
    return Principal(
        oid=oid,
        email="user@example.com",
        role=role,
        raw_claims={} if raw_claims is None else raw_claims,
    )


def _tenant_service(tenants: list[Tenant]) -> MagicMock:
    svc = MagicMock()
    svc.list = MagicMock(return_value=tenants)
    return svc


def _graph_service(*, group_ids=None, raises: bool = False) -> AsyncMock:
    graph = AsyncMock()
    if raises:
        graph.list_member_group_ids = AsyncMock(
            side_effect=GraphError(502, "graph_down")
        )
    else:
        graph.list_member_group_ids = AsyncMock(return_value=list(group_ids or []))
    return graph


def _resolver(tenant_service, graph_service, **kwargs) -> TenantResolver:
    return TenantResolver(tenant_service, graph_service, **kwargs)


# --- admin bypass -----------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_is_global_even_when_graph_raises():
    """ADMIN → is_global=True immediately; a raising Graph (fallback path) must
    NEVER fail an admin's resolve — memberships degrade to empty, no exception."""
    tenants = [_tenant(id="ten-1", name="Retail", group_ids=["grp-retail"])]
    graph = _graph_service(raises=True)
    resolver = _resolver(_tenant_service(tenants), graph)

    ctx = await resolver.resolve(_principal(role=Role.ADMIN, oid="admin-oid"))

    assert ctx.is_global is True
    assert ctx.tenant_ids == frozenset()
    assert ctx.tenants == ()


@pytest.mark.asyncio
async def test_admin_memberships_resolved_best_effort_for_display():
    """ADMIN with a matching groups claim → is_global True AND memberships still
    resolved (for /users/me display)."""
    tenants = [_tenant(id="ten-1", name="Retail", group_ids=["grp-retail"])]
    graph = _graph_service()
    resolver = _resolver(_tenant_service(tenants), graph)

    ctx = await resolver.resolve(
        _principal(role=Role.ADMIN, oid="admin-oid", raw_claims={"groups": ["grp-retail"]})
    )

    assert ctx.is_global is True
    assert ctx.tenant_ids == frozenset({"ten-1"})
    assert [t.id for t in ctx.tenants] == ["ten-1"]
    graph.list_member_group_ids.assert_not_called()


# --- group-source precedence ------------------------------------------------


@pytest.mark.asyncio
async def test_groups_claim_wins_graph_not_called():
    """The token ``groups`` claim wins — Graph is NEVER called when it is present."""
    tenants = [_tenant(id="ten-1", name="Retail", group_ids=["grp-retail"])]
    graph = _graph_service()
    resolver = _resolver(_tenant_service(tenants), graph)

    ctx = await resolver.resolve(
        _principal(role=Role.OPERATOR, oid="user-oid", raw_claims={"groups": ["grp-retail"]})
    )

    assert ctx.is_global is False
    assert ctx.tenant_ids == frozenset({"ten-1"})
    graph.list_member_group_ids.assert_not_called()


@pytest.mark.asyncio
async def test_groups_claim_present_but_empty_yields_empty_no_graph():
    """A PRESENT-but-empty ``groups`` claim is authoritative → empty set, and Graph
    is NOT consulted (precedence is by key presence, not truthiness)."""
    tenants = [_tenant(id="ten-1", name="Retail", group_ids=["grp-retail"])]
    graph = _graph_service(group_ids=["grp-retail"])
    resolver = _resolver(_tenant_service(tenants), graph)

    ctx = await resolver.resolve(
        _principal(role=Role.OPERATOR, oid="user-oid", raw_claims={"groups": []})
    )

    assert ctx.tenant_ids == frozenset()
    graph.list_member_group_ids.assert_not_called()


@pytest.mark.asyncio
async def test_graph_fallback_used_when_claim_absent():
    """No ``groups`` key + a truthy oid → fall back to Graph transitiveMemberOf."""
    tenants = [_tenant(id="ten-1", name="Retail", group_ids=["grp-retail"])]
    graph = _graph_service(group_ids=["grp-retail"])
    resolver = _resolver(_tenant_service(tenants), graph)

    ctx = await resolver.resolve(_principal(role=Role.OPERATOR, oid="user-oid"))

    assert ctx.tenant_ids == frozenset({"ten-1"})
    graph.list_member_group_ids.assert_awaited_once_with("user-oid")


@pytest.mark.asyncio
async def test_graph_error_yields_empty_set_no_raise():
    """A raising Graph fallback (GraphError) degrades to an empty set — never a raise."""
    tenants = [_tenant(id="ten-1", name="Retail", group_ids=["grp-retail"])]
    graph = _graph_service(raises=True)
    resolver = _resolver(_tenant_service(tenants), graph)

    ctx = await resolver.resolve(_principal(role=Role.OPERATOR, oid="user-oid"))

    assert ctx.is_global is False
    assert ctx.tenant_ids == frozenset()
    assert ctx.tenants == ()


@pytest.mark.asyncio
async def test_dev_auth_non_admin_yields_empty_set():
    """dev-auth non-admin principal (oid=None, raw_claims={}) → empty set; Graph
    is NOT called (no oid to resolve)."""
    tenants = [_tenant(id="ten-1", name="Retail", group_ids=["grp-retail"])]
    graph = _graph_service(group_ids=["grp-retail"])
    resolver = _resolver(_tenant_service(tenants), graph)

    ctx = await resolver.resolve(_principal(role=Role.VIEWER, oid=None, raw_claims={}))

    assert ctx.is_global is False
    assert ctx.tenant_ids == frozenset()
    graph.list_member_group_ids.assert_not_called()


@pytest.mark.asyncio
async def test_multi_tenant_user_gets_both_ids_and_records():
    """A user whose groups match two tenants → both ids + both Tenant records."""
    tenants = [
        _tenant(id="ten-1", name="Retail", group_ids=["grp-retail"]),
        _tenant(id="ten-2", name="Wholesale", group_ids=["grp-wholesale"]),
        _tenant(id="ten-3", name="Markets", group_ids=["grp-markets"]),
    ]
    graph = _graph_service()
    resolver = _resolver(_tenant_service(tenants), graph)

    ctx = await resolver.resolve(
        _principal(
            role=Role.OPERATOR,
            oid="user-oid",
            raw_claims={"groups": ["grp-retail", "grp-wholesale"]},
        )
    )

    assert ctx.tenant_ids == frozenset({"ten-1", "ten-2"})
    assert {t.id for t in ctx.tenants} == {"ten-1", "ten-2"}


# --- visible() truth table --------------------------------------------------


def _ctx(*, is_global: bool, tenant_ids: frozenset[str]) -> TenantContext:
    return TenantContext(is_global=is_global, tenant_ids=tenant_ids, tenants=())


def test_visible_global_sees_everything():
    ctx = _ctx(is_global=True, tenant_ids=frozenset())
    assert visible(ctx, "ten-anything") is True
    assert visible(ctx, None) is True
    assert visible(ctx, "ten-foreign", shared=False) is True


def test_visible_member_sees_own_tenant():
    ctx = _ctx(is_global=False, tenant_ids=frozenset({"ten-1"}))
    assert visible(ctx, "ten-1") is True


def test_visible_non_member_blocked_on_foreign_tenant():
    ctx = _ctx(is_global=False, tenant_ids=frozenset({"ten-1"}))
    assert visible(ctx, "ten-foreign") is False


def test_visible_shared_resource_visible_to_foreign_caller():
    ctx = _ctx(is_global=False, tenant_ids=frozenset({"ten-1"}))
    assert visible(ctx, "ten-foreign", shared=True) is True


def test_visible_none_tenant_id_is_false_for_non_global():
    ctx = _ctx(is_global=False, tenant_ids=frozenset({"ten-1"}))
    assert visible(ctx, None) is False
    # ...but shared=True still wins even with a None tenant_id.
    assert visible(ctx, None, shared=True) is True


# --- resolve_oid_tenants (E24/T7 — the cross-tenant grant guard's primitive) --


@pytest.mark.asyncio
async def test_resolve_oid_tenants_intersects_groups_with_tenants():
    """An arbitrary oid's Graph groups are intersected against the tenant list —
    the same group→tenant logic ``resolve`` uses, keyed by the GRANTEE's oid."""
    tenants = [
        _tenant(id="ten-1", name="Retail", group_ids=["grp-retail"]),
        _tenant(id="ten-2", name="Wholesale", group_ids=["grp-wholesale"]),
    ]
    graph = _graph_service(group_ids=["grp-retail"])
    resolver = _resolver(_tenant_service(tenants), graph)

    result = await resolver.resolve_oid_tenants("grantee-oid")

    assert result == frozenset({"ten-1"})
    assert isinstance(result, frozenset)
    graph.list_member_group_ids.assert_awaited_once_with("grantee-oid")


@pytest.mark.asyncio
async def test_resolve_oid_tenants_multi_tenant_grantee():
    tenants = [
        _tenant(id="ten-1", name="Retail", group_ids=["grp-retail"]),
        _tenant(id="ten-2", name="Wholesale", group_ids=["grp-wholesale"]),
        _tenant(id="ten-3", name="Markets", group_ids=["grp-markets"]),
    ]
    graph = _graph_service(group_ids=["grp-retail", "grp-wholesale"])
    resolver = _resolver(_tenant_service(tenants), graph)

    assert await resolver.resolve_oid_tenants("grantee-oid") == frozenset(
        {"ten-1", "ten-2"}
    )


@pytest.mark.asyncio
async def test_resolve_oid_tenants_graph_error_returns_empty_frozenset():
    """GraphError → empty frozenset (fail-closed: the guard treats an unresolvable
    grantee as cross-tenant), never a raise."""
    tenants = [_tenant(id="ten-1", name="Retail", group_ids=["grp-retail"])]
    graph = _graph_service(raises=True)
    resolver = _resolver(_tenant_service(tenants), graph)

    assert await resolver.resolve_oid_tenants("grantee-oid") == frozenset()


@pytest.mark.asyncio
async def test_resolve_oid_tenants_no_matching_groups_empty():
    tenants = [_tenant(id="ten-1", name="Retail", group_ids=["grp-retail"])]
    graph = _graph_service(group_ids=["grp-unrelated"])
    resolver = _resolver(_tenant_service(tenants), graph)

    assert await resolver.resolve_oid_tenants("grantee-oid") == frozenset()


@pytest.mark.asyncio
async def test_resolve_oid_tenants_group_oid_matches_entra_group_ids_directly():
    """A GROUP grantee IS the group: its oid sitting directly in a tenant's
    ``entra_group_ids`` resolves that tenant — no Graph resolve required (the E6
    Access tab grants groups too; T7-fix)."""
    tenants = [
        _tenant(id="ten-1", name="Retail", group_ids=["grp-retail"]),
        _tenant(id="ten-2", name="Wholesale", group_ids=["grp-wholesale"]),
    ]
    graph = _graph_service(group_ids=[])  # a group has no /users/{oid} memberships
    resolver = _resolver(_tenant_service(tenants), graph)

    assert await resolver.resolve_oid_tenants("grp-retail") == frozenset({"ten-1"})


@pytest.mark.asyncio
async def test_resolve_oid_tenants_group_oid_direct_match_survives_graph_error():
    """A GROUP oid 404s on /users/{oid}/transitiveMemberOf (GraphError) — the user
    path degrades but the DIRECT entra_group_ids match still resolves the tenant
    (no raise). This is the T7-fix regression case: same-tenant group grants must
    not fail-closed to ADMIN."""
    tenants = [_tenant(id="ten-1", name="Retail", group_ids=["grp-retail"])]
    graph = _graph_service(raises=True)
    resolver = _resolver(_tenant_service(tenants), graph)

    assert await resolver.resolve_oid_tenants("grp-retail") == frozenset({"ten-1"})


@pytest.mark.asyncio
async def test_resolve_oid_tenants_unions_user_path_and_direct_group_path():
    """The user path (Graph memberships) and the direct-group path (the oid itself
    in ``entra_group_ids``) are UNIONED — an oid matching via both yields both
    tenants."""
    tenants = [
        _tenant(id="ten-1", name="Retail", group_ids=["grp-retail"]),
        _tenant(id="ten-2", name="Wholesale", group_ids=["the-oid"]),
    ]
    graph = _graph_service(group_ids=["grp-retail"])
    resolver = _resolver(_tenant_service(tenants), graph)

    assert await resolver.resolve_oid_tenants("the-oid") == frozenset(
        {"ten-1", "ten-2"}
    )


@pytest.mark.asyncio
async def test_resolve_oid_tenants_unlinked_group_oid_empty_even_on_graph_error():
    """A GROUP oid linked to NO tenant + a raising Graph → empty frozenset
    (fail-closed: the guard requires ADMIN), never a raise."""
    tenants = [_tenant(id="ten-1", name="Retail", group_ids=["grp-retail"])]
    graph = _graph_service(raises=True)
    resolver = _resolver(_tenant_service(tenants), graph)

    assert await resolver.resolve_oid_tenants("grp-unlinked") == frozenset()


@pytest.mark.asyncio
async def test_resolve_oid_tenants_uses_ttl_cached_tenant_list():
    """Reuses the resolver's TTL-cached tenant list — two calls inside the TTL
    window read tenant_service.list() ONCE (same cache ``resolve`` uses)."""
    tenants = [_tenant(id="ten-1", name="Retail", group_ids=["grp-retail"])]
    ts = _tenant_service(tenants)
    clock = iter([100.0, 130.0])
    resolver = _resolver(
        ts, _graph_service(group_ids=["grp-retail"]), tenant_list_ttl_seconds=60, now=lambda: next(clock)
    )

    await resolver.resolve_oid_tenants("grantee-oid")
    await resolver.resolve_oid_tenants("grantee-oid")

    assert ts.list.call_count == 1


# --- TTL cache on tenant_service.list() -------------------------------------


@pytest.mark.asyncio
async def test_tenant_list_cached_within_ttl():
    """Two resolves within the TTL window → tenant_service.list() called ONCE."""
    tenants = [_tenant(id="ten-1", name="Retail", group_ids=["grp-retail"])]
    ts = _tenant_service(tenants)
    clock = iter([100.0, 130.0])  # both within a 60s TTL of the first read
    resolver = _resolver(ts, _graph_service(), tenant_list_ttl_seconds=60, now=lambda: next(clock))

    await resolver.resolve(_principal(role=Role.OPERATOR, oid="u", raw_claims={"groups": ["grp-retail"]}))
    await resolver.resolve(_principal(role=Role.OPERATOR, oid="u", raw_claims={"groups": ["grp-retail"]}))

    assert ts.list.call_count == 1


@pytest.mark.asyncio
async def test_tenant_list_refreshed_after_ttl():
    """A resolve after the TTL expires → tenant_service.list() called AGAIN."""
    tenants = [_tenant(id="ten-1", name="Retail", group_ids=["grp-retail"])]
    ts = _tenant_service(tenants)
    clock = iter([100.0, 200.0])  # second read is >60s later → cache expired
    resolver = _resolver(ts, _graph_service(), tenant_list_ttl_seconds=60, now=lambda: next(clock))

    await resolver.resolve(_principal(role=Role.OPERATOR, oid="u", raw_claims={"groups": ["grp-retail"]}))
    await resolver.resolve(_principal(role=Role.OPERATOR, oid="u", raw_claims={"groups": ["grp-retail"]}))

    assert ts.list.call_count == 2
