# tests/test_project_resolver.py — MagicMock service returning REAL records + AsyncMock graph.
# Repo is NOT in pytest-asyncio auto mode: every async test needs an explicit marker.
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from core.rbac import Principal, Role
from models.project_role import ProjectRole, ProjectRoleRecord
from services.graph_service import GraphError
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
        project_id=project_id, principal_id=principal_id, principal_type=ptype,
        principal_display="P", role=role, granted_by="seed", granted_at=FIXED_TS,
    )


def _principal(*, oid="oid-1", role=Role.OPERATOR, claims=None) -> Principal:
    return Principal(oid=oid, email="a@example.com", role=role, raw_claims=claims if claims is not None else {})


def _resolver(rows, group_ids=None, graph_raises=False):
    svc = MagicMock()
    svc.list_all.return_value = rows
    graph = AsyncMock()
    if graph_raises:
        # GraphError's signature is (status, code, message=None) — see services/graph_service.py.
        graph.list_member_group_ids.side_effect = GraphError(502, "graph_down")
    else:
        graph.list_member_group_ids.return_value = group_ids or []
    return ProjectResolver(svc, graph)


# --- may() is pure -----------------------------------------------------------

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


# --- resolve() ---------------------------------------------------------------

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
    svc = MagicMock()
    svc.list_all.return_value = [_row("proj-1", "oid-1", "owner")]
    graph = AsyncMock()
    graph.list_member_group_ids.return_value = []
    clock = {"t": 1000.0}
    r = ProjectResolver(svc, graph, role_list_ttl_seconds=60, now=lambda: clock["t"])

    await r.resolve(_principal(claims={"groups": []}))
    await r.resolve(_principal(claims={"groups": []}))
    assert svc.list_all.call_count == 1          # second call served from cache

    clock["t"] += 61
    await r.resolve(_principal(claims={"groups": []}))
    assert svc.list_all.call_count == 2          # TTL expired -> re-read


@pytest.mark.asyncio
async def test_unknown_role_name_grants_nothing():
    """An out-of-band row (hand-edited, or written by a future version that adds a role
    name) must grant NOTHING — never silently default to a level."""
    r = _resolver([_row("proj-1", "oid-1", "superadmin")])
    ctx = await r.resolve(_principal(claims={"groups": []}))
    assert ctx.roles == {}
    assert "proj-1" not in ctx.roles
    assert may(ctx, "proj-1", ProjectRole.VIEWER) is False


@pytest.mark.asyncio
async def test_rows_for_one_project_never_influence_another():
    """The max-fold is PER PROJECT — a strong grant on one project must not leak into
    another (a VIEWER on a sandbox must never read as OWNER on production)."""
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
    with pytest.raises(TypeError):
        ctx.roles["hijack"] = ProjectRole.OWNER  # type: ignore[index]
    assert may(ctx, "hijack", ProjectRole.OWNER) is False


@pytest.mark.asyncio
async def test_invalidate_drops_the_cache_so_a_revoke_is_seen_immediately():
    """A role write calls invalidate() to collapse the stale-grant window to zero."""
    svc = MagicMock()
    svc.list_all.return_value = [_row("proj-1", "oid-1", "owner")]
    graph = AsyncMock()
    graph.list_member_group_ids.return_value = []
    clock = {"t": 1000.0}
    r = ProjectResolver(svc, graph, role_list_ttl_seconds=60, now=lambda: clock["t"])

    ctx = await r.resolve(_principal(claims={"groups": []}))
    assert may(ctx, "proj-1", ProjectRole.OWNER) is True

    svc.list_all.return_value = []               # the grant is revoked in the store
    r.invalidate()
    ctx = await r.resolve(_principal(claims={"groups": []}))  # same instant, no TTL wait
    assert ctx.roles == {}
    assert svc.list_all.call_count == 2

    r.invalidate()                               # idempotent on an already-empty cache
    r.invalidate()


# ===========================================================================
# The MULTI-PROCESS staleness window (E27 fix pass, review I2)
#
# ``invalidate()`` is process-local and ``ecs_desired_count`` is 2, so a grant written
# through one ECS task leaves the OTHER task's cache pre-grant for up to a TTL. These
# simulate that with TWO resolver instances over ONE shared store — the shape a
# single-process suite is otherwise structurally blind to.
# ===========================================================================

class _SharedStore:
    """One role store behind two resolvers, mimicking two ECS tasks over one DDB table."""

    def __init__(self, rows=None):
        self.rows = list(rows or [])

    def list_all(self):
        return list(self.rows)

    def list_for_project_strict(self, project_id):
        return [r for r in self.rows if r.project_id == project_id]


def _two_tasks(store):
    """Two independent resolvers (= two processes) sharing ONE store, one frozen clock so
    nothing expires by elapsed time — only an explicit invalidate/refresh can change an answer."""
    graph = AsyncMock()
    graph.list_member_group_ids.return_value = []
    # Frozen clock: the TTL must never be what rescues either task.
    def now():
        return 1000.0

    return (
        ProjectResolver(store, graph, role_list_ttl_seconds=60, now=now),
        ProjectResolver(store, graph, role_list_ttl_seconds=60, now=now),
    )


@pytest.mark.asyncio
async def test_a_second_task_serves_a_stale_empty_role_cache_after_a_grant():
    """Pins the DEFECT this fix exists for, so nobody 'simplifies' the refresh away.

    Task B warmed its cache before the grant. Task A then grants and invalidates ITSELF.
    B's cached resolve still says no — that is the 60s window, and it is why a gate must not
    treat a cached DENIAL as final."""
    store = _SharedStore()
    task_a, task_b = _two_tasks(store)
    principal = _principal(oid="creator-oid", claims={"groups": []})

    await task_b.resolve(principal)                      # B warms an EMPTY snapshot
    store.rows.append(_row("proj-new", "creator-oid", "owner"))  # the creator-OWNER grant
    task_a.invalidate()                                  # only A's cache is dropped

    assert may(await task_a.resolve(principal), "proj-new", ProjectRole.OWNER) is True
    # B is STALE — the bug. Without the refresh below this is the answer the gate serves.
    assert may(await task_b.resolve(principal), "proj-new", ProjectRole.OWNER) is False


@pytest.mark.asyncio
async def test_refresh_project_sees_a_grant_the_stale_cache_missed():
    """The fix: a FRESH single-project read on task B reports the role its cache cannot see.

    This is the assertion that FAILS under the pre-fix behaviour (no refresh existed, so the
    caller's only answer was the stale ``False`` above)."""
    store = _SharedStore()
    task_a, task_b = _two_tasks(store)
    principal = _principal(oid="creator-oid", claims={"groups": []})

    await task_b.resolve(principal)
    store.rows.append(_row("proj-new", "creator-oid", "owner"))
    task_a.invalidate()

    fresh = task_b.refresh_project(principal, "proj-new")
    assert may(fresh, "proj-new", ProjectRole.OWNER) is True
    # ...and it did NOT poison B's cache: the refresh is a per-request second opinion only.
    assert may(await task_b.resolve(principal), "proj-new", ProjectRole.OWNER) is False


@pytest.mark.asyncio
async def test_refresh_project_reads_only_the_one_project():
    """Scoped to a ``begins_with`` range read — never the whole partition, since this runs on
    a deny path that any caller can reach."""
    store = MagicMock()
    store.list_for_project_strict.return_value = [_row("proj-1", "oid-1", "owner")]
    graph = AsyncMock()
    resolver = ProjectResolver(store, graph)

    ctx = resolver.refresh_project(_principal(claims={"groups": []}), "proj-1")
    assert may(ctx, "proj-1", ProjectRole.OWNER) is True
    store.list_for_project_strict.assert_called_once_with("proj-1")
    store.list_all.assert_not_called()


def test_refresh_project_matches_a_group_grant_from_the_claim():
    """A group-derived role must refresh too — the groups-first design's whole point."""
    store = MagicMock()
    store.list_for_project_strict.return_value = [
        _row("proj-1", "grp-A", "owner", ptype="group")
    ]
    resolver = ProjectResolver(store, AsyncMock())
    ctx = resolver.refresh_project(_principal(claims={"groups": ["grp-A"]}), "proj-1")
    assert may(ctx, "proj-1", ProjectRole.OWNER) is True


def test_refresh_project_propagates_a_store_fault():
    """Strict read: 'unreadable' must not read as 'no role' silently. The route catches this
    and lets the original refusal stand."""
    from services.project_role_service import ProjectRoleError

    store = MagicMock()
    store.list_for_project_strict.side_effect = ProjectRoleError(
        "boom", kind="ownership_unverified"
    )
    resolver = ProjectResolver(store, AsyncMock())
    with pytest.raises(ProjectRoleError):
        resolver.refresh_project(_principal(claims={"groups": []}), "proj-1")


# --- context_from_rows / widen are pure -------------------------------------

def test_context_from_rows_folds_already_read_rows_without_touching_the_store():
    """The LIST routes' path: they have already read the partition, so the fold costs no read."""
    store = MagicMock()
    ctx = context_from_rows(
        _principal(oid="oid-1", claims={"groups": []}),
        [_row("proj-1", "oid-1", "maintainer"), _row("proj-2", "other-oid", "owner")],
    )
    assert ctx.roles == {"proj-1": ProjectRole.MAINTAINER}   # only the caller's own rows
    store.list_all.assert_not_called()


def test_context_from_rows_is_read_only_and_admin_aware():
    ctx = context_from_rows(_principal(role=Role.ADMIN, claims={"groups": []}), [])
    assert ctx.is_global is True
    with pytest.raises(TypeError):
        ctx.roles["hijack"] = ProjectRole.OWNER  # type: ignore[index]


def test_widen_takes_the_max_and_never_lowers_a_role():
    """The property that makes the refresh safe: widening can only ADD authority.

    A group-derived grant that ONLY the cached (Graph-backed) resolve can see must survive a
    claims-only refresh — otherwise the fix for a hidden button would hide a real role."""
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
        "proj-1": ProjectRole.OWNER,        # NOT lowered to viewer
        "proj-2": ProjectRole.VIEWER,       # absent from extra, still kept
        "proj-3": ProjectRole.MAINTAINER,   # added by extra
    }


def test_widen_keeps_a_global_admin_global():
    base = ProjectContext(is_global=True, roles={})
    assert widen(base, ProjectContext(is_global=False, roles={})).is_global is True
