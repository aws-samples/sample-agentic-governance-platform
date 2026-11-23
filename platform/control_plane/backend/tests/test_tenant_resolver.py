"""The resolver turns a validated ``Principal`` into a ``TenantContext``, the primitive every
scoped route gates on through ``visible()``.

Fakes: a ``MagicMock`` tenant service whose ``.list()`` returns real ``Tenant`` records, so the
group intersection is exercised, and an ``AsyncMock`` graph service, so no live Graph is touched.
Every async test is decorated ``@pytest.mark.asyncio`` explicitly; the repo is not in auto mode.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from core.rbac import Principal, Role
from models.tenant import Tenant, TenantStageConfig
from services.graph_errors import GraphError
from services.tenant_resolver import TenantContext, TenantResolver, visible

FIXED_TS = "2026-07-13T00:00:00+00:00"


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
        graph.list_member_group_ids = AsyncMock(side_effect=GraphError(502, "graph_down"))
    else:
        graph.list_member_group_ids = AsyncMock(return_value=list(group_ids or []))
    return graph


def _resolver(tenant_service, graph_service, **kwargs) -> TenantResolver:
    return TenantResolver(tenant_service, graph_service, **kwargs)


@pytest.mark.asyncio
async def test_admin_is_global_even_when_graph_raises():
    """A raising Graph never fails an admin's resolve; memberships degrade to empty."""
    tenants = [_tenant(id="ten-1", name="Retail", group_ids=["grp-retail"])]
    graph = _graph_service(raises=True)
    resolver = _resolver(_tenant_service(tenants), graph)

    ctx = await resolver.resolve(_principal(role=Role.ADMIN, oid="admin-oid"))

    assert ctx.is_global is True
    assert ctx.tenant_ids == frozenset()
    assert ctx.tenants == ()


@pytest.mark.asyncio
async def test_admin_memberships_resolved_best_effort_for_display():
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


@pytest.mark.asyncio
async def test_groups_claim_wins_graph_not_called():
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
    """Precedence is by key presence, not truthiness."""
    tenants = [_tenant(id="ten-1", name="Retail", group_ids=["grp-retail"])]
    graph = _graph_service(group_ids=["grp-retail"])
    resolver = _resolver(_tenant_service(tenants), graph)

    ctx = await resolver.resolve(_principal(role=Role.OPERATOR, oid="user-oid", raw_claims={"groups": []}))

    assert ctx.tenant_ids == frozenset()
    graph.list_member_group_ids.assert_not_called()


@pytest.mark.asyncio
async def test_graph_fallback_used_when_claim_absent():
    tenants = [_tenant(id="ten-1", name="Retail", group_ids=["grp-retail"])]
    graph = _graph_service(group_ids=["grp-retail"])
    resolver = _resolver(_tenant_service(tenants), graph)

    ctx = await resolver.resolve(_principal(role=Role.OPERATOR, oid="user-oid"))

    assert ctx.tenant_ids == frozenset({"ten-1"})
    graph.list_member_group_ids.assert_awaited_once_with("user-oid")


@pytest.mark.asyncio
async def test_graph_error_yields_empty_set_no_raise():
    tenants = [_tenant(id="ten-1", name="Retail", group_ids=["grp-retail"])]
    graph = _graph_service(raises=True)
    resolver = _resolver(_tenant_service(tenants), graph)

    ctx = await resolver.resolve(_principal(role=Role.OPERATOR, oid="user-oid"))

    assert ctx.is_global is False
    assert ctx.tenant_ids == frozenset()
    assert ctx.tenants == ()


@pytest.mark.asyncio
async def test_dev_auth_non_admin_yields_empty_set():
    """A dev-auth principal has no oid and no claims, so Graph is not called."""
    tenants = [_tenant(id="ten-1", name="Retail", group_ids=["grp-retail"])]
    graph = _graph_service(group_ids=["grp-retail"])
    resolver = _resolver(_tenant_service(tenants), graph)

    ctx = await resolver.resolve(_principal(role=Role.VIEWER, oid=None, raw_claims={}))

    assert ctx.is_global is False
    assert ctx.tenant_ids == frozenset()
    graph.list_member_group_ids.assert_not_called()


@pytest.mark.asyncio
async def test_multi_tenant_user_gets_both_ids_and_records():
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


def test_visible_none_tenant_id_is_false_for_non_global_unless_shared():
    ctx = _ctx(is_global=False, tenant_ids=frozenset({"ten-1"}))
    assert visible(ctx, None) is False
    assert visible(ctx, None, shared=True) is True


@pytest.mark.asyncio
async def test_resolve_oid_tenants_intersects_groups_with_tenants():
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

    assert await resolver.resolve_oid_tenants("grantee-oid") == frozenset({"ten-1", "ten-2"})


@pytest.mark.asyncio
async def test_resolve_oid_tenants_graph_error_returns_empty_frozenset():
    """An unresolvable grantee is treated as cross-tenant, never a raise."""
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
    """A group grantee is the group: its oid in a tenant's ``entra_group_ids`` resolves that
    tenant with no Graph memberships of its own."""
    tenants = [
        _tenant(id="ten-1", name="Retail", group_ids=["grp-retail"]),
        _tenant(id="ten-2", name="Wholesale", group_ids=["grp-wholesale"]),
    ]
    graph = _graph_service(group_ids=[])
    resolver = _resolver(_tenant_service(tenants), graph)

    assert await resolver.resolve_oid_tenants("grp-retail") == frozenset({"ten-1"})


@pytest.mark.asyncio
async def test_resolve_oid_tenants_group_oid_direct_match_survives_graph_error():
    """A group oid 404s on the user memberships read; the direct match still resolves the tenant,
    so a same-tenant group grant does not fail closed to admin-only."""
    tenants = [_tenant(id="ten-1", name="Retail", group_ids=["grp-retail"])]
    graph = _graph_service(raises=True)
    resolver = _resolver(_tenant_service(tenants), graph)

    assert await resolver.resolve_oid_tenants("grp-retail") == frozenset({"ten-1"})


@pytest.mark.asyncio
async def test_resolve_oid_tenants_unions_user_path_and_direct_group_path():
    tenants = [
        _tenant(id="ten-1", name="Retail", group_ids=["grp-retail"]),
        _tenant(id="ten-2", name="Wholesale", group_ids=["the-oid"]),
    ]
    graph = _graph_service(group_ids=["grp-retail"])
    resolver = _resolver(_tenant_service(tenants), graph)

    assert await resolver.resolve_oid_tenants("the-oid") == frozenset({"ten-1", "ten-2"})


@pytest.mark.asyncio
async def test_resolve_oid_tenants_unlinked_group_oid_empty_even_on_graph_error():
    tenants = [_tenant(id="ten-1", name="Retail", group_ids=["grp-retail"])]
    graph = _graph_service(raises=True)
    resolver = _resolver(_tenant_service(tenants), graph)

    assert await resolver.resolve_oid_tenants("grp-unlinked") == frozenset()


@pytest.mark.asyncio
async def test_resolve_oid_tenants_uses_ttl_cached_tenant_list():
    tenants = [_tenant(id="ten-1", name="Retail", group_ids=["grp-retail"])]
    ts = _tenant_service(tenants)
    clock = iter([100.0, 130.0])
    resolver = _resolver(
        ts, _graph_service(group_ids=["grp-retail"]), tenant_list_ttl_seconds=60, now=lambda: next(clock)
    )

    await resolver.resolve_oid_tenants("grantee-oid")
    await resolver.resolve_oid_tenants("grantee-oid")

    assert ts.list.call_count == 1


@pytest.mark.asyncio
async def test_tenant_list_cached_within_ttl():
    tenants = [_tenant(id="ten-1", name="Retail", group_ids=["grp-retail"])]
    ts = _tenant_service(tenants)
    clock = iter([100.0, 130.0])
    resolver = _resolver(ts, _graph_service(), tenant_list_ttl_seconds=60, now=lambda: next(clock))

    await resolver.resolve(_principal(role=Role.OPERATOR, oid="u", raw_claims={"groups": ["grp-retail"]}))
    await resolver.resolve(_principal(role=Role.OPERATOR, oid="u", raw_claims={"groups": ["grp-retail"]}))

    assert ts.list.call_count == 1


@pytest.mark.asyncio
async def test_tenant_list_refreshed_after_ttl():
    tenants = [_tenant(id="ten-1", name="Retail", group_ids=["grp-retail"])]
    ts = _tenant_service(tenants)
    clock = iter([100.0, 200.0])
    resolver = _resolver(ts, _graph_service(), tenant_list_ttl_seconds=60, now=lambda: next(clock))

    await resolver.resolve(_principal(role=Role.OPERATOR, oid="u", raw_claims={"groups": ["grp-retail"]}))
    await resolver.resolve(_principal(role=Role.OPERATOR, oid="u", raw_claims={"groups": ["grp-retail"]}))

    assert ts.list.call_count == 2
