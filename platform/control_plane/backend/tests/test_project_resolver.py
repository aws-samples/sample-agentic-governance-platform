"""ProjectResolver over a MagicMock role service returning real records and an AsyncMock Graph client.
The suite is not in pytest-asyncio auto mode, so every async test carries the marker."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.rbac import Principal, Role
from models.project_role import ProjectRole, ProjectRoleRecord
from services.graph_errors import GraphError
from services.project_resolver import (
    ProjectContext,
    ProjectResolver,
    context_from_rows,
    may,
    widen,
)

FIXED_TS = "2026-07-27T00:00:00+00:00"


def _row(project_id: str, principal_id: str, role: str, ptype: str = "user") -> ProjectRoleRecord:
    return ProjectRoleRecord(
        project_id=project_id,
        principal_id=principal_id,
        principal_type=ptype,
        principal_display="P",
        role=role,
        granted_by="seed",
        granted_at=FIXED_TS,
    )


def _principal(*, oid="oid-1", role=Role.OPERATOR, claims=None) -> Principal:
    return Principal(
        oid=oid, email="a@example.com", role=role, raw_claims=claims if claims is not None else {}
    )


def _resolver(rows, group_ids=None, graph_raises=False):
    svc = MagicMock()
    svc.list_all.return_value = rows
    graph = AsyncMock()
    if graph_raises:
        graph.list_member_group_ids.side_effect = GraphError(502, "graph_down")
    else:
        graph.list_member_group_ids.return_value = group_ids or []
    return ProjectResolver(svc, graph)


def test_may_admin_always_passes():
    ctx = ProjectContext(is_global=True, roles={})
    assert may(ctx, "proj-1", ProjectRole.OWNER) is True


def test_may_is_fail_closed_for_unknown_project_and_none():
    ctx = ProjectContext(is_global=False, roles={"proj-1": ProjectRole.OWNER})
    assert may(ctx, "proj-2", ProjectRole.VIEWER) is False
    assert may(ctx, None, ProjectRole.VIEWER) is False


def test_may_is_a_threshold_not_an_equality():
    ctx = ProjectContext(is_global=False, roles={"p": ProjectRole.MAINTAINER})
    assert may(ctx, "p", ProjectRole.VIEWER) is True
    assert may(ctx, "p", ProjectRole.MAINTAINER) is True
    assert may(ctx, "p", ProjectRole.OWNER) is False


@pytest.mark.asyncio
async def test_direct_user_grant_resolves():
    r = _resolver([_row("proj-1", "oid-1", "maintainer")])
    ctx = await r.resolve(_principal(claims={"groups": []}))
    assert ctx.roles == {"proj-1": ProjectRole.MAINTAINER}
    assert ctx.is_global is False


@pytest.mark.asyncio
async def test_group_grant_resolves_via_claim():
    r = _resolver([_row("proj-1", "grp-A", "owner", ptype="group")])
    ctx = await r.resolve(_principal(claims={"groups": ["grp-A"]}))
    assert ctx.roles == {"proj-1": ProjectRole.OWNER}


@pytest.mark.asyncio
async def test_effective_role_is_the_max_across_matches():
    rows = [
        _row("proj-1", "oid-1", "viewer"),
        _row("proj-1", "grp-A", "owner", ptype="group"),
    ]
    r = _resolver(rows)
    ctx = await r.resolve(_principal(claims={"groups": ["grp-A"]}))
    assert ctx.roles == {"proj-1": ProjectRole.OWNER}


@pytest.mark.asyncio
async def test_present_but_empty_groups_claim_skips_graph():
    r = _resolver([_row("proj-1", "grp-A", "owner", ptype="group")], group_ids=["grp-A"])
    ctx = await r.resolve(_principal(claims={"groups": []}))
    assert ctx.roles == {}
    r._graph_service.list_member_group_ids.assert_not_called()


@pytest.mark.asyncio
async def test_absent_groups_claim_falls_back_to_graph():
    r = _resolver([_row("proj-1", "grp-A", "owner", ptype="group")], group_ids=["grp-A"])
    ctx = await r.resolve(_principal(claims={}))
    assert ctx.roles == {"proj-1": ProjectRole.OWNER}
    r._graph_service.list_member_group_ids.assert_awaited_once()


@pytest.mark.asyncio
async def test_graph_error_degrades_to_no_groups():
    r = _resolver([_row("proj-1", "grp-A", "owner", ptype="group")], graph_raises=True)
    ctx = await r.resolve(_principal(claims={}))
    assert ctx.roles == {}


@pytest.mark.asyncio
async def test_admin_is_global():
    r = _resolver([])
    ctx = await r.resolve(_principal(role=Role.ADMIN, claims={"groups": []}))
    assert ctx.is_global is True
    assert may(ctx, "anything", ProjectRole.OWNER) is True


@pytest.mark.asyncio
async def test_no_rows_is_empty_not_an_error():
    r = _resolver([])
    ctx = await r.resolve(_principal(claims={"groups": []}))
    assert ctx.roles == {}


@pytest.mark.asyncio
async def test_role_list_is_ttl_cached():
    """The second resolve within the TTL is served from the cache; one past the TTL re-reads."""
    svc = MagicMock()
    svc.list_all.return_value = [_row("proj-1", "oid-1", "owner")]
    graph = AsyncMock()
    graph.list_member_group_ids.return_value = []
    clock = {"t": 1000.0}
    r = ProjectResolver(svc, graph, role_list_ttl_seconds=60, now=lambda: clock["t"])

    await r.resolve(_principal(claims={"groups": []}))
    await r.resolve(_principal(claims={"groups": []}))
    assert svc.list_all.call_count == 1

    clock["t"] += 61
    await r.resolve(_principal(claims={"groups": []}))
    assert svc.list_all.call_count == 2


@pytest.mark.asyncio
async def test_unknown_role_name_grants_nothing():
    """An out-of-band row (hand-edited, or written by a future version that adds a role name) grants
    nothing rather than defaulting to a level."""
    r = _resolver([_row("proj-1", "oid-1", "superadmin")])
    ctx = await r.resolve(_principal(claims={"groups": []}))
    assert ctx.roles == {}
    assert "proj-1" not in ctx.roles
    assert may(ctx, "proj-1", ProjectRole.VIEWER) is False


@pytest.mark.asyncio
async def test_rows_for_one_project_never_influence_another():
    """The fold is per project: a strong grant on one project must not leak into another."""
    rows = [_row("proj-A", "oid-1", "owner"), _row("proj-B", "oid-1", "viewer")]
    r = _resolver(rows)
    ctx = await r.resolve(_principal(claims={"groups": []}))
    assert ctx.roles == {"proj-A": ProjectRole.OWNER, "proj-B": ProjectRole.VIEWER}
    assert may(ctx, "proj-A", ProjectRole.OWNER) is True
    assert may(ctx, "proj-B", ProjectRole.OWNER) is False
    assert may(ctx, "proj-B", ProjectRole.VIEWER) is True


@pytest.mark.asyncio
async def test_resolved_roles_map_is_read_only():
    """The context is a trust boundary: a holder must not be able to write itself a grant."""
    r = _resolver([_row("proj-1", "oid-1", "viewer")])
    ctx = await r.resolve(_principal(claims={"groups": []}))
    untyped: Any = ctx.roles
    with pytest.raises(TypeError):
        untyped["hijack"] = ProjectRole.OWNER
    assert may(ctx, "hijack", ProjectRole.OWNER) is False


@pytest.mark.asyncio
async def test_invalidate_drops_the_cache_so_a_revoke_is_seen_immediately():
    """A role write calls ``invalidate()`` to collapse the stale-grant window to zero; it is idempotent
    on an already-empty cache."""
    svc = MagicMock()
    svc.list_all.return_value = [_row("proj-1", "oid-1", "owner")]
    graph = AsyncMock()
    graph.list_member_group_ids.return_value = []
    clock = {"t": 1000.0}
    r = ProjectResolver(svc, graph, role_list_ttl_seconds=60, now=lambda: clock["t"])

    ctx = await r.resolve(_principal(claims={"groups": []}))
    assert may(ctx, "proj-1", ProjectRole.OWNER) is True

    svc.list_all.return_value = []
    r.invalidate()
    ctx = await r.resolve(_principal(claims={"groups": []}))
    assert ctx.roles == {}
    assert svc.list_all.call_count == 2

    r.invalidate()
    r.invalidate()


class _SharedStore:
    """One role store behind two resolvers, mimicking two ECS tasks over one DynamoDB table."""

    def __init__(self, rows=None):
        self.rows = list(rows or [])

    def list_all(self):
        return list(self.rows)

    def list_for_project_strict(self, project_id):
        return [r for r in self.rows if r.project_id == project_id]


def _two_tasks(store):
    """Two independent resolvers (two processes) sharing one store on one frozen clock, so nothing
    expires by elapsed time and only an explicit invalidate or refresh can change an answer."""
    graph = AsyncMock()
    graph.list_member_group_ids.return_value = []

    def now():
        return 1000.0

    return (
        ProjectResolver(store, graph, role_list_ttl_seconds=60, now=now),
        ProjectResolver(store, graph, role_list_ttl_seconds=60, now=now),
    )


@pytest.mark.asyncio
async def test_a_second_task_serves_a_stale_empty_role_cache_after_a_grant():
    """Pins the defect the refresh exists for: task B warmed its cache before the grant, task A grants
    and invalidates itself, and B's cached resolve still says no for up to a TTL."""
    store = _SharedStore()
    task_a, task_b = _two_tasks(store)
    principal = _principal(oid="creator-oid", claims={"groups": []})

    await task_b.resolve(principal)
    store.rows.append(_row("proj-new", "creator-oid", "owner"))
    task_a.invalidate()

    assert may(await task_a.resolve(principal), "proj-new", ProjectRole.OWNER) is True
    assert may(await task_b.resolve(principal), "proj-new", ProjectRole.OWNER) is False


@pytest.mark.asyncio
async def test_refresh_project_sees_a_grant_the_stale_cache_missed():
    """A fresh single-project read on task B reports the role its cache cannot see, and does not
    poison B's cache: the refresh is a per-request second opinion only."""
    store = _SharedStore()
    task_a, task_b = _two_tasks(store)
    principal = _principal(oid="creator-oid", claims={"groups": []})

    await task_b.resolve(principal)
    store.rows.append(_row("proj-new", "creator-oid", "owner"))
    task_a.invalidate()

    fresh = task_b.refresh_project(principal, "proj-new")
    assert may(fresh, "proj-new", ProjectRole.OWNER) is True
    assert may(await task_b.resolve(principal), "proj-new", ProjectRole.OWNER) is False


@pytest.mark.asyncio
async def test_refresh_project_reads_only_the_one_project():
    """A ``begins_with`` range read, never the whole partition, since it runs on a deny path any
    caller can reach."""
    store = MagicMock()
    store.list_for_project_strict.return_value = [_row("proj-1", "oid-1", "owner")]
    graph = AsyncMock()
    resolver = ProjectResolver(store, graph)

    ctx = resolver.refresh_project(_principal(claims={"groups": []}), "proj-1")
    assert may(ctx, "proj-1", ProjectRole.OWNER) is True
    store.list_for_project_strict.assert_called_once_with("proj-1")
    store.list_all.assert_not_called()


def test_refresh_project_matches_a_group_grant_from_the_claim():
    store = MagicMock()
    store.list_for_project_strict.return_value = [_row("proj-1", "grp-A", "owner", ptype="group")]
    resolver = ProjectResolver(store, AsyncMock())
    ctx = resolver.refresh_project(_principal(claims={"groups": ["grp-A"]}), "proj-1")
    assert may(ctx, "proj-1", ProjectRole.OWNER) is True


def test_refresh_project_propagates_a_store_fault():
    """Strict read: "unreadable" must not read as "no role"; the route lets the refusal stand."""
    from services.project_role_service import ProjectRoleError

    store = MagicMock()
    store.list_for_project_strict.side_effect = ProjectRoleError("boom", kind="ownership_unverified")
    resolver = ProjectResolver(store, AsyncMock())
    with pytest.raises(ProjectRoleError):
        resolver.refresh_project(_principal(claims={"groups": []}), "proj-1")


def test_context_from_rows_folds_already_read_rows_without_touching_the_store():
    """The list routes' path: they have already read the partition, so the fold costs no read and keeps
    only the caller's own rows."""
    store = MagicMock()
    ctx = context_from_rows(
        _principal(oid="oid-1", claims={"groups": []}),
        [_row("proj-1", "oid-1", "maintainer"), _row("proj-2", "other-oid", "owner")],
    )
    assert ctx.roles == {"proj-1": ProjectRole.MAINTAINER}
    store.list_all.assert_not_called()


def test_context_from_rows_is_read_only_and_admin_aware():
    ctx = context_from_rows(_principal(role=Role.ADMIN, claims={"groups": []}), [])
    assert ctx.is_global is True
    untyped: Any = ctx.roles
    with pytest.raises(TypeError):
        untyped["hijack"] = ProjectRole.OWNER


def test_widen_takes_the_max_and_never_lowers_a_role():
    """Widening can only add authority: an owner stays owner, a role absent from ``extra`` is kept, and
    a role only ``extra`` carries is added."""
    base = ProjectContext(
        is_global=False,
        roles={"proj-1": ProjectRole.OWNER, "proj-2": ProjectRole.VIEWER},
    )
    extra = ProjectContext(
        is_global=False,
        roles={"proj-1": ProjectRole.VIEWER, "proj-3": ProjectRole.MAINTAINER},
    )
    merged = widen(base, extra)
    assert merged.roles == {
        "proj-1": ProjectRole.OWNER,
        "proj-2": ProjectRole.VIEWER,
        "proj-3": ProjectRole.MAINTAINER,
    }


def test_widen_keeps_a_global_admin_global():
    base = ProjectContext(is_global=True, roles={})
    assert widen(base, ProjectContext(is_global=False, roles={})).is_global is True
