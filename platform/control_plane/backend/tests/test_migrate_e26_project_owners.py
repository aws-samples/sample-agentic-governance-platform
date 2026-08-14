"""Offline tests for ``scripts/migrate_e26_project_owners.py`` (E27/T6).

Pure-logic tests via injected collaborators (NO moto, NO boto3, NO live AWS, NO live
Graph): ``migrate()`` takes ``projects``/``roles``/``graph`` as parameters, so no client
is ever constructed here. ``graph`` is an ``AsyncMock`` because
``GraphService.resolve_user_by_email`` is ``async``; ``projects``/``roles`` are
``MagicMock``s. Mirrors ``test_seed_default_tenant.py`` / ``test_migrate_to_e25b.py``'s
``sys.path`` shim that makes ``scripts.<mod>`` importable.

Creator resolution goes through ``GraphService.resolve_user_by_email`` — the EXACT
``$filter`` lookup — NOT ``search_principals``, which only ``$search``es ``displayName``
and therefore resolves no address at all. The mock returns the same hit shape
(``{id, displayName, type, mail}``); a list is used where a test needs to drive the
"several principals share this address" path.

The repo is NOT in pytest-asyncio auto mode, so every async test carries
``@pytest.mark.asyncio`` explicitly.

The ``roles`` fake wires ``has_role_rows`` to ``bool(list_for_project(...))`` — the
REAL service's own relationship (``has_role_rows`` is the strict ``list_for_project``
turned into a bool) — so a test can express "this project is already governed" by
setting ``list_for_project.return_value`` alone, exactly as the pinned tests do.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

# Backend dir on sys.path makes `scripts` importable as a namespace package (same shim
# as test_seed_default_tenant.py / test_migrate_to_e25b.py).
_BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

from models.project import Project  # noqa: E402 — after sys.path shim
from models.project_role import ProjectRoleRecord  # noqa: E402
from scripts.migrate_e26_project_owners import migrate  # noqa: E402

PROJECT_ID = "proj-1"
CREATOR_EMAIL = "a@example.com"


def _project(**overrides) -> Project:
    data = {
        "id": PROJECT_ID,
        "name": "Claims",
        "connection_id": "conn-1",
        "tenant_id": "default",
        "created_by": CREATOR_EMAIL,
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    data.update(overrides)
    return Project(**data)


def _governed_row(role: str = "owner") -> ProjectRoleRecord:
    """An existing role row — what makes a project ALREADY governed."""
    return ProjectRoleRecord(
        project_id=PROJECT_ID,
        principal_id="oid-existing",
        principal_type="user",
        principal_display="Existing Owner",
        role=role,
        granted_by="admin-oid",
        granted_at="2026-07-27T00:00:00+00:00",
    )


@pytest.fixture
def projects():
    """``ProjectService`` stand-in returning ONE real ``Project``."""
    svc = MagicMock(name="ProjectService")
    svc.list_projects.return_value = [_project()]
    return svc


@pytest.fixture
def roles():
    """``ProjectRoleService`` stand-in; ungoverned by default (no role rows).

    ``has_role_rows`` is derived from ``list_for_project`` so it can never disagree
    with it — the same invariant the real service has.
    """
    svc = MagicMock(name="ProjectRoleService")
    svc.list_for_project.return_value = []
    svc.has_role_rows.side_effect = lambda project_id: bool(
        svc.list_for_project(project_id)
    )
    return svc


@pytest.fixture
def graph():
    """``GraphService`` stand-in — ``resolve_user_by_email`` is ``async``.

    Defaults to "no such user" (the real method returns ``None`` when the exact
    ``$filter`` matches no user, or more than one).
    """
    svc = AsyncMock(name="GraphService")
    svc.resolve_user_by_email.return_value = None
    return svc


@pytest.mark.asyncio
async def test_dry_run_grants_nothing(projects, roles, graph):
    graph.resolve_user_by_email.return_value = {"id": "oid-1", "mail": "a@example.com", "type": "user"}
    result = await migrate(projects=projects, roles=roles, graph=graph, apply=False)
    roles.grant.assert_not_called()
    assert result.would_grant == 1


@pytest.mark.asyncio
async def test_apply_grants_owner_from_resolved_email(projects, roles, graph):
    graph.resolve_user_by_email.return_value = {
        "id": "oid-1", "mail": "a@example.com", "type": "user", "displayName": "Alex"
    }
    result = await migrate(projects=projects, roles=roles, graph=graph, apply=True)
    assert roles.grant.call_args.args[1].principal_id == "oid-1"
    assert roles.grant.call_args.args[1].role == "owner"
    assert result.granted == 1
    # MINOR-1/2: pin the provenance and the principal type on the written row. A machine
    # backfill must never be recorded as a human grant, and the row must say "user"
    # (a group oid here would grant OWNER to every member — see the group-hit test).
    assert roles.grant.call_args.kwargs["granted_by"] == "migration:e26-project-owners"
    assert roles.grant.call_args.args[1].principal_type == "user"


@pytest.mark.asyncio
async def test_already_governed_project_is_skipped(projects, roles, graph):
    roles.list_for_project.return_value = [_governed_row()]
    result = await migrate(projects=projects, roles=roles, graph=graph, apply=True)
    roles.grant.assert_not_called()
    graph.resolve_user_by_email.assert_not_awaited()  # no Graph call for a governed project
    assert result.skipped_governed == 1


@pytest.mark.asyncio
async def test_ambiguous_email_is_reported_not_guessed(projects, roles, graph):
    graph.resolve_user_by_email.return_value = [
        {"id": "oid-1", "mail": "a@example.com", "type": "user"},
        {"id": "oid-2", "mail": "a@example.com", "type": "user"},
    ]
    result = await migrate(projects=projects, roles=roles, graph=graph, apply=True)
    roles.grant.assert_not_called()
    assert "a@example.com" in result.unresolved


@pytest.mark.asyncio
async def test_unresolvable_email_is_reported(projects, roles, graph):
    graph.resolve_user_by_email.return_value = None
    result = await migrate(projects=projects, roles=roles, graph=graph, apply=True)
    roles.grant.assert_not_called()
    assert result.unresolved == ["a@example.com"]


@pytest.mark.asyncio
async def test_non_exact_match_is_not_accepted(projects, roles, graph):
    """A $search hit on a DIFFERENT address must never be treated as the creator."""
    graph.resolve_user_by_email.return_value = {
        "id": "oid-9", "mail": "alex.other@example.com", "type": "user"
    }
    result = await migrate(projects=projects, roles=roles, graph=graph, apply=True)
    roles.grant.assert_not_called()
    assert result.unresolved == ["a@example.com"]


@pytest.mark.asyncio
async def test_rerun_after_apply_is_idempotent(projects, roles, graph):
    graph.resolve_user_by_email.return_value = {"id": "oid-1", "mail": "a@example.com", "type": "user"}
    await migrate(projects=projects, roles=roles, graph=graph, apply=True)
    roles.list_for_project.return_value = [_governed_row()]
    second = await migrate(projects=projects, roles=roles, graph=graph, apply=True)
    assert second.granted == 0


# ---------------------------------------------------------------------------
# Beyond the pinned set — the two decisions the brief pinned for this task.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lookup_uses_the_exact_email_resolver(projects, roles, graph):
    """The creator's ADDRESS is resolved by the exact ``$filter`` lookup.

    ``search_principals`` only ``$search``es ``displayName``, so it can never resolve an
    address — using it would leave essentially every project unresolved. The address is
    passed through verbatim.
    """
    graph.resolve_user_by_email.return_value = {
        "id": "oid-1", "mail": "a@example.com", "type": "user"
    }
    await migrate(projects=projects, roles=roles, graph=graph, apply=True)
    assert graph.resolve_user_by_email.await_args.args[0] == CREATOR_EMAIL
    graph.search_principals.assert_not_awaited()      # the fuzzy picker is NOT the resolver


@pytest.mark.asyncio
async def test_group_typed_hit_is_never_granted_owner(projects, roles, graph):
    """A mail-enabled GROUP sharing the creator's address must be REFUSED.

    Groups carry ``mail`` in Entra, and the row is written ``principal_type="user"`` — a
    group oid in it would grant OWNER to every member while misreporting the blast
    radius. The refusal must be the script's own, not a side effect of what the resolver
    happens to return.
    """
    graph.resolve_user_by_email.return_value = {
        "id": "grp-1", "mail": "a@example.com", "type": "group"
    }
    result = await migrate(projects=projects, roles=roles, graph=graph, apply=True)
    roles.grant.assert_not_called()
    assert result.granted == 0
    assert result.unresolved == [CREATOR_EMAIL]


@pytest.mark.asyncio
async def test_user_principal_name_counts_as_an_exact_match(projects, roles, graph):
    """Graph hits often carry no ``mail`` — ``userPrincipalName`` is the other exact key."""
    graph.resolve_user_by_email.return_value = {
        "id": "oid-1",
        "mail": None,
        "userPrincipalName": "A@Example.com",
        "type": "user",
    }
    result = await migrate(projects=projects, roles=roles, graph=graph, apply=True)
    assert roles.grant.call_args.args[1].principal_id == "oid-1"
    assert result.granted == 1


@pytest.mark.asyncio
async def test_blank_created_by_is_unresolved_not_a_crash(projects, roles, graph):
    projects.list_projects.return_value = [_project(created_by="")]
    result = await migrate(projects=projects, roles=roles, graph=graph, apply=True)
    roles.grant.assert_not_called()
    graph.resolve_user_by_email.assert_not_awaited()  # nothing to look up
    assert result.unresolved == ["(blank)"]
    assert result.granted == 0


@pytest.mark.asyncio
async def test_created_by_already_an_oid_is_used_directly(projects, roles, graph):
    """Some records store the creator's oid, not an email — usable without Graph."""
    oid = "11111111-2222-3333-4444-555555555555"
    projects.list_projects.return_value = [_project(created_by=oid)]
    result = await migrate(projects=projects, roles=roles, graph=graph, apply=True)
    graph.resolve_user_by_email.assert_not_awaited()  # already an oid — no lookup needed
    assert roles.grant.call_args.args[1].principal_id == oid
    assert result.granted == 1
    assert result.used_oid_directly == [oid]           # ...and the summary says so


@pytest.mark.asyncio
async def test_unverifiable_governance_never_grants(projects, roles, graph):
    """A strict read that RAISES means governed-or-not is UNKNOWN — fail closed."""
    from services.project_role_service import ProjectRoleError

    roles.has_role_rows.side_effect = ProjectRoleError(
        "Could not verify project ownership", kind="ownership_unverified"
    )
    graph.resolve_user_by_email.return_value = {
        "id": "oid-1", "mail": "a@example.com", "type": "user"
    }
    result = await migrate(projects=projects, roles=roles, graph=graph, apply=True)
    roles.grant.assert_not_called()
    graph.resolve_user_by_email.assert_not_awaited()
    assert result.unresolved == ["a@example.com"]
    assert result.granted == 0


@pytest.mark.asyncio
async def test_project_filter_limits_the_pass_to_one_project(projects, roles, graph):
    projects.list_projects.return_value = [_project(), _project(id="proj-2")]
    graph.resolve_user_by_email.return_value = {
        "id": "oid-1", "mail": "a@example.com", "type": "user"
    }
    result = await migrate(
        projects=projects, roles=roles, graph=graph, apply=True, project_id="proj-2"
    )
    assert result.granted == 1
    assert roles.grant.call_args.args[0] == "proj-2"


@pytest.mark.asyncio
async def test_a_failed_grant_is_counted_and_does_not_abort(projects, roles, graph):
    projects.list_projects.return_value = [_project(), _project(id="proj-2")]
    graph.resolve_user_by_email.return_value = {
        "id": "oid-1", "mail": "a@example.com", "type": "user"
    }
    roles.grant.side_effect = [RuntimeError("ddb down"), MagicMock()]
    result = await migrate(projects=projects, roles=roles, graph=graph, apply=True)
    assert roles.grant.call_count == 2      # the second project still ran
    assert result.granted == 1
    assert result.failures == 1
