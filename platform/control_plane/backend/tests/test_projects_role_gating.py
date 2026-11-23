"""Per-project role gating of the project and repository routes.

Fixture idiom is ``test_project_roles_routes.py``'s: the module reset, the entra env fixture,
fake resolvers on the ``users`` module globals, the services as ``MagicMock``s on the ``projects``
module globals, and no ``dependency_overrides``, so the real ``require_role`` / ``current_principal`` /
``get_tenant_ctx`` / ``get_project_ctx`` chain runs against a mocked ``verify_entra_token``.

Pinned here: the required role per route (viewer reads, maintainer materializes, owner destroys) and
that a caller one level below it gets 403 with the fixed literal; repo delete needs owner and is refused
before the cascade runs; the ungoverned-project rule (a project with zero role rows keeps maintainer-level
verbs open to tenant members, owner verbs still demand a real row); ``_load_visible_project`` runs first,
so a foreign tenant still 404s before any role logic; the two list routes filter to the projects the
caller may see with one role-store read; a global platform admin bypasses the project role; the detail
read's two UI hints agree with the gates; the multi-task cache window and its deny-path refresh; the role
rows' cleanup on project delete; and promote's error table through the one handler.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from models.project import Project, ProjectDetail
from models.project_role import ROLE_NAMES, ProjectRoleRecord
from models.repository import (
    RepoDeleteItemResult,
    RepoDeletePreview,
    RepoDeletePreviewItem,
    RepoDeleteResult,
    Repository,
)

pytestmark = pytest.mark.usefixtures("reset_modules")

FIXED_TS = "2026-07-27T00:00:00+00:00"
INSUFFICIENT = "insufficient project role"


def _governed_row(pid="proj-1", principal="someone-else-oid", role="owner"):
    """A role row on ``pid``; its existence is what makes the project governed."""
    return ProjectRoleRecord(
        project_id=pid,
        principal_id=principal,
        principal_type="user",
        principal_display="Alex",
        role=role,
        granted_by="seed",
        granted_at=FIXED_TS,
    )


def _project(id="proj-1", tenant_id="default"):
    return Project(
        id=id,
        name="Fraud Ops",
        connection_id="conn-1",
        tenant_id=tenant_id,
        description="a container",
        created_by="operator@x.com",
        created_at=FIXED_TS,
        updated_at=FIXED_TS,
    )


def _detail(project=None):
    return ProjectDetail(project=project or _project(), repositories=[])


def _repository(id="r-1", project_id="proj-1"):
    return Repository(
        id=id,
        project_id=project_id,
        name="fraud-agent",
        repo_url="https://github.com/acme/fraud-agent",
        agent_id="a-1",
        template_name="strands-agentcore",
        cicd_status="provisioning",
        status="provisioning",
        created_by="operator@x.com",
        created_at=FIXED_TS,
        updated_at=FIXED_TS,
    )


def _valid_repo_body():
    return {
        "name": "fraud-agent",
        "template_name": "strands-agentcore",
        "agent_config": {"framework": "strands", "agent_name": "fraud_agent"},
    }


def _tenant_context(*, is_global=False, tenant_ids=("default",)):
    from services.tenant_resolver import TenantContext

    return TenantContext(is_global=is_global, tenant_ids=frozenset(tenant_ids), tenants=())


def _as_role(value):
    return ROLE_NAMES[value] if isinstance(value, str) else value


def _project_context(*, is_global=False, role=None, project_id="proj-1", roles=None):
    from services.project_resolver import ProjectContext

    if roles is not None:
        mapped = {pid: _as_role(r) for pid, r in roles.items()}
    elif role is None:
        mapped = {}
    else:
        mapped = {project_id: _as_role(role)}
    return ProjectContext(is_global=is_global, roles=mapped)


class _FakeTenantResolver:
    def __init__(self, ctx):
        self._ctx = ctx

    async def resolve(self, principal):
        return self._ctx


class _FakeProjectResolver:
    """Async ``resolve`` stub plus the real ``refresh_project`` semantics: the gates call it on every
    deny, so it reads the same ``_role_svc`` the test seeds through the real ``context_from_rows`` fold,
    which lets a stale-cache test seed rows the cached context omits."""

    def __init__(self, ctx):
        self._ctx = ctx
        self.invalidate = MagicMock()

    async def resolve(self, principal):
        return self._ctx

    def refresh_project(self, principal, project_id):
        import api.routes.projects as projects_module
        from services.project_resolver import context_from_rows

        assert projects_module._role_svc is not None
        rows = projects_module._role_svc.list_for_project_strict(project_id)
        return context_from_rows(principal, rows)


class _FakeTenantService:
    def __init__(self, known_ids):
        self._known = set(known_ids)

    def get(self, tenant_id):
        from services.tenant_service import TenantError

        if tenant_id not in self._known:
            raise TenantError("Unknown tenant", kind="not_found")
        return MagicMock(id=tenant_id)


_CLAIMS = {
    "viewer": "Platform.Viewer",
    "operator": "Platform.Operator",
    "admin": "Platform.Admin",
}


def _claims_for(platform_role: str):
    return {
        "oid": f"{platform_role}-oid",
        "preferred_username": f"{platform_role}@x.com",
        "roles": [_CLAIMS[platform_role]],
    }


@pytest.fixture
def role_svc_with_both_read_pairs():
    """The ``ProjectRoleService`` MagicMock on the projects module global, defaulting to an ungoverned
    project (zero rows). The gate reads the strict pair (``has_role_rows`` / ``list_all_strict``); the
    degrading pair is seeded too so a switch back to it shows up as a governance change rather than a
    silent pass on a truthy auto-attribute."""
    import api.routes.projects as projects_module

    svc = MagicMock()
    svc.has_role_rows.return_value = False
    svc.list_all_strict.return_value = []
    svc.list_for_project.return_value = []
    svc.list_all.return_value = []
    projects_module._role_svc = svc
    return svc


def _governed(role_svc, *pids):
    """Mark the given project ids governed for both gate inputs."""
    ids = set(pids) or {"proj-1"}
    role_svc.has_role_rows.side_effect = lambda pid: pid in ids
    role_svc.list_all_strict.return_value = [_governed_row(pid=p) for p in sorted(ids)]


@pytest.fixture
def mock_project_svc_with_a_repo():
    """The ``ProjectService`` MagicMock on the projects module global, defaulting to a visible project
    with a repo under it so the tenant gate passes and the role gate is what's under test."""
    import api.routes.projects as projects_module

    svc = MagicMock()
    svc.get_project.return_value = _detail()
    svc.create_project.return_value = _project()
    svc.list_projects.return_value = [_project()]
    svc.get_repo.return_value = _repository()
    svc.add_repo.return_value = _repository()
    svc.retry_materialize.return_value = _repository()
    svc.list_repositories.return_value = [_repository()]
    svc.delete_project.return_value = None
    svc.preview_delete.return_value = RepoDeletePreview(
        items=[RepoDeletePreviewItem(item="github", state="present")]
    )
    svc.delete_repo.return_value = RepoDeleteResult(
        items=[RepoDeleteItemResult(item="record", outcome="deleted")], record_removed=True
    )
    projects_module._svc = svc
    return svc


@pytest.fixture
def client_factory_with_repositories(
    entra_settings, role_svc_with_both_read_pairs, mock_project_svc_with_a_repo
):
    """Build a TestClient for a caller with a given platform role and project role, with the repositories
    router mounted beside the project routers and a tenant service that knows ``default``, ``ten-1`` and
    ``ten-2``."""
    built = []

    def _make(*, platform_role="operator", project_role=None, roles=None, project_ctx=None, tenant_ctx=None):
        import api.routes.project_repos as project_repos_module
        import api.routes.projects as projects_module
        import api.routes.repositories as repositories_module
        import api.routes.tenants as tenants_module
        import api.routes.users as users_module
        from core.errors import install_error_handlers

        users_module._tenant_resolver = _FakeTenantResolver(
            tenant_ctx if tenant_ctx is not None else _tenant_context()
        )
        users_module._project_resolver = _FakeProjectResolver(
            project_ctx
            if project_ctx is not None
            else _project_context(is_global=platform_role == "admin", role=project_role, roles=roles)
        )
        tenants_module._svc = _FakeTenantService({"default", "ten-1", "ten-2"})

        app = FastAPI()
        install_error_handlers(app)
        app.include_router(projects_module.router, prefix="/api/v1")
        app.include_router(project_repos_module.router, prefix="/api/v1")
        app.include_router(repositories_module.repositories_router, prefix="/api/v1")
        client = TestClient(app, headers={"Authorization": "Bearer fake-token"})
        patcher = patch("core.security_entra.verify_entra_token", return_value=_claims_for(platform_role))
        patcher.start()
        client._entra_patcher = patcher
        built.append(client)
        return client

    yield _make
    for client in built:
        client._entra_patcher.stop()


_GATED = [
    ("get", "/api/v1/projects/proj-1", None, "viewer"),
    ("get", "/api/v1/projects/proj-1/repos/r-1/status", None, "viewer"),
    ("post", "/api/v1/projects/proj-1/repos", _valid_repo_body(), "maintainer"),
    ("post", "/api/v1/projects/proj-1/repos/r-1/retry", None, "maintainer"),
    ("get", "/api/v1/projects/proj-1/repos/r-1/delete-preview", None, "owner"),
    ("delete", "/api/v1/projects/proj-1/repos/r-1", None, "owner"),
    ("delete", "/api/v1/projects/proj-1", None, "owner"),
]

_ONE_BELOW = {"viewer": None, "maintainer": "viewer", "owner": "maintainer"}


def _call(client, method, path, body):
    """One request, table-driven; a bodyless verb goes through the plain method and a bodied one
    through ``request``, since ``httpx``'s ``get`` and ``delete`` take no ``json``."""
    if body is None:
        return getattr(client, method)(path)
    return client.request(method.upper(), path, json=body)


@pytest.mark.parametrize("method,path,body,required_role", _GATED)
def test_route_requires_its_role(
    method, path, body, required_role, client_factory_with_repositories, role_svc_with_both_read_pairs
):
    """A caller one level below the required role is refused with the fixed literal."""
    below = _ONE_BELOW[required_role]
    if below is None:
        pytest.skip("viewer is the floor")
    _governed(role_svc_with_both_read_pairs, "proj-1")
    client = client_factory_with_repositories(project_role=below)
    r = _call(client, method, path, body)
    assert r.status_code == 403
    assert r.json() == {"detail": INSUFFICIENT, "code": "project_role.insufficient"}


@pytest.mark.parametrize("method,path,body,required_role", _GATED)
def test_route_admits_exactly_its_role(
    method, path, body, required_role, client_factory_with_repositories, role_svc_with_both_read_pairs
):
    """The gate is a threshold, not a wall."""
    _governed(role_svc_with_both_read_pairs, "proj-1")
    client = client_factory_with_repositories(project_role=required_role)
    r = _call(client, method, path, body)
    assert r.status_code < 400, r.text


@pytest.mark.parametrize("method,path,body,required_role", _GATED)
def test_no_project_role_on_a_governed_project_is_refused(
    method, path, body, required_role, client_factory_with_repositories, role_svc_with_both_read_pairs
):
    """Fail closed on a governed project: rows exist but none are the caller's, so even the viewer
    floor is a 403."""
    _governed(role_svc_with_both_read_pairs, "proj-1")
    client = client_factory_with_repositories(project_role=None)
    r = _call(client, method, path, body)
    assert r.status_code == 403
    assert r.json() == {"detail": INSUFFICIENT, "code": "project_role.insufficient"}


def test_repo_delete_requires_owner_not_maintainer(
    client_factory_with_repositories, role_svc_with_both_read_pairs, mock_project_svc_with_a_repo
):
    """The teardown cascade is an owner act, refused before the cascade runs."""
    _governed(role_svc_with_both_read_pairs, "proj-1")
    client = client_factory_with_repositories(project_role="maintainer")
    r = client.delete("/api/v1/projects/proj-1/repos/r-1")
    assert r.status_code == 403
    mock_project_svc_with_a_repo.delete_repo.assert_not_called()


def test_project_delete_requires_owner_not_maintainer(
    client_factory_with_repositories, role_svc_with_both_read_pairs, mock_project_svc_with_a_repo
):
    _governed(role_svc_with_both_read_pairs, "proj-1")
    client = client_factory_with_repositories(project_role="maintainer")
    assert client.delete("/api/v1/projects/proj-1").status_code == 403
    mock_project_svc_with_a_repo.delete_project.assert_not_called()


def test_gate_is_scoped_per_project_not_globally_held(
    client_factory_with_repositories, role_svc_with_both_read_pairs, mock_project_svc_with_a_repo
):
    """Owner on proj-1 is not owner on proj-2."""
    mock_project_svc_with_a_repo.get_project.return_value = _detail(_project(id="proj-2"))
    _governed(role_svc_with_both_read_pairs, "proj-2")
    client = client_factory_with_repositories(roles={"proj-1": "owner"})
    r = client.delete("/api/v1/projects/proj-2")
    assert r.status_code == 403
    mock_project_svc_with_a_repo.delete_project.assert_not_called()


@pytest.mark.parametrize("method,path,body,required_role", _GATED)
def test_foreign_tenant_404s_before_any_role_logic(
    method,
    path,
    body,
    required_role,
    client_factory_with_repositories,
    role_svc_with_both_read_pairs,
    mock_project_svc_with_a_repo,
):
    """A foreign tenant's project must look absent, never 403, so the role store is never consulted. The
    caller holds no role and the project is governed, so only ``_load_visible_project`` running first can
    produce the 404."""
    mock_project_svc_with_a_repo.get_project.return_value = _detail(_project(tenant_id="ten-2"))
    _governed(role_svc_with_both_read_pairs, "proj-1")
    client = client_factory_with_repositories(
        project_role=None, tenant_ctx=_tenant_context(tenant_ids=["ten-1"])
    )
    r = _call(client, method, path, body)
    assert r.status_code == 404
    assert r.json() == {"detail": "Project not found", "code": "project.not_found"}
    role_svc_with_both_read_pairs.has_role_rows.assert_not_called()


@pytest.mark.parametrize("method,path,body,required_role", _GATED)
def test_gate_order_is_observable_on_every_gated_route(
    method,
    path,
    body,
    required_role,
    client_factory_with_repositories,
    role_svc_with_both_read_pairs,
    mock_project_svc_with_a_repo,
):
    """The two gates disagree here (tenant-first says 404, role-first says 403), so the response
    distinguishes the order; byte-identical to the truly-missing 404 is the property."""
    mock_project_svc_with_a_repo.get_project.return_value = _detail(_project(tenant_id="ten-2"))
    _governed(role_svc_with_both_read_pairs, "proj-1")
    client = client_factory_with_repositories(
        project_role=None, tenant_ctx=_tenant_context(tenant_ids=["ten-1"])
    )
    foreign = _call(client, method, path, body)

    mock_project_svc_with_a_repo.get_project.return_value = None
    missing = _call(client, method, path.replace("proj-1", "truly-missing"), body)

    assert foreign.status_code == missing.status_code == 404, foreign.text
    assert foreign.json() == missing.json() == {"detail": "Project not found", "code": "project.not_found"}


def test_foreign_tenant_404_is_byte_identical_to_a_missing_project(
    client_factory_with_repositories, role_svc_with_both_read_pairs, mock_project_svc_with_a_repo
):
    mock_project_svc_with_a_repo.get_project.return_value = _detail(_project(tenant_id="ten-2"))
    _governed(role_svc_with_both_read_pairs, "proj-1")
    client = client_factory_with_repositories(
        project_role=None, tenant_ctx=_tenant_context(tenant_ids=["ten-1"])
    )
    foreign = client.delete("/api/v1/projects/proj-1")
    mock_project_svc_with_a_repo.get_project.return_value = None
    missing = client.delete("/api/v1/projects/truly-missing")
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json() == {"detail": "Project not found", "code": "project.not_found"}


def test_ungoverned_project_allows_maintainer_verbs(
    client_factory_with_repositories, role_svc_with_both_read_pairs, mock_project_svc_with_a_repo
):
    """Zero role rows keeps the pre-migration semantics, so migration is not a flag day."""
    role_svc_with_both_read_pairs.has_role_rows.return_value = False
    client = client_factory_with_repositories(project_role=None)
    r = client.post("/api/v1/projects/proj-1/repos", json=_valid_repo_body())
    assert r.status_code == 202
    mock_project_svc_with_a_repo.add_repo.assert_called_once()


def test_ungoverned_project_allows_viewer_verbs(
    client_factory_with_repositories, role_svc_with_both_read_pairs
):
    role_svc_with_both_read_pairs.has_role_rows.return_value = False
    client = client_factory_with_repositories(project_role=None)
    assert client.get("/api/v1/projects/proj-1").status_code == 200
    assert client.get("/api/v1/projects/proj-1/repos/r-1/status").status_code == 200


def test_ungoverned_project_still_blocks_owner_verbs(
    client_factory_with_repositories, role_svc_with_both_read_pairs, mock_project_svc_with_a_repo
):
    role_svc_with_both_read_pairs.has_role_rows.return_value = False
    client = client_factory_with_repositories(project_role=None)
    r = client.delete("/api/v1/projects/proj-1")
    assert r.status_code == 403
    assert r.json() == {"detail": INSUFFICIENT, "code": "project_role.insufficient"}
    mock_project_svc_with_a_repo.delete_project.assert_not_called()


def test_ungoverned_project_still_blocks_the_repo_delete_cascade(
    client_factory_with_repositories, role_svc_with_both_read_pairs, mock_project_svc_with_a_repo
):
    """The most destructive verb never rides the ungoverned rule."""
    role_svc_with_both_read_pairs.has_role_rows.return_value = False
    client = client_factory_with_repositories(project_role="maintainer")
    assert client.delete("/api/v1/projects/proj-1/repos/r-1").status_code == 403
    assert client.get("/api/v1/projects/proj-1/repos/r-1/delete-preview").status_code == 403
    mock_project_svc_with_a_repo.delete_repo.assert_not_called()
    mock_project_svc_with_a_repo.preview_delete.assert_not_called()


def test_governed_path_costs_no_extra_store_read(
    client_factory_with_repositories, role_svc_with_both_read_pairs
):
    """``has_rows`` is consulted only when ``may()`` already said no."""
    client = client_factory_with_repositories(project_role="owner")
    assert client.delete("/api/v1/projects/proj-1").status_code == 204
    role_svc_with_both_read_pairs.has_role_rows.assert_not_called()


def test_malformed_project_id_fails_closed(
    client_factory_with_repositories, role_svc_with_both_read_pairs, mock_project_svc_with_a_repo
):
    """An id the store cannot even read rows for (``kind="validation"``) is not treated as ungoverned."""
    from services.project_role_service import ProjectRoleError

    mock_project_svc_with_a_repo.get_project.return_value = _detail(_project(id="proj#1"))
    role_svc_with_both_read_pairs.has_role_rows.side_effect = ProjectRoleError("bad id", kind="validation")
    client = client_factory_with_repositories(project_role=None)
    r = client.post("/api/v1/projects/proj%231/repos", json=_valid_repo_body())
    assert r.status_code == 403
    mock_project_svc_with_a_repo.add_repo.assert_not_called()


def _unverifiable(role_svc):
    """Both strict gate reads fail the way an unreadable DynamoDB partition surfaces."""
    from services.project_role_service import ProjectRoleError

    boom = ProjectRoleError("Could not verify project ownership", kind="ownership_unverified")
    role_svc.has_role_rows.side_effect = boom
    role_svc.list_all_strict.side_effect = boom


def test_store_read_failure_does_not_grant_the_fallback_on_a_write(
    client_factory_with_repositories, role_svc_with_both_read_pairs, mock_project_svc_with_a_repo
):
    """A role-less caller must not get 202 into someone else's project because the role partition was
    briefly unreadable."""
    _unverifiable(role_svc_with_both_read_pairs)
    client = client_factory_with_repositories(project_role=None)
    r = client.post("/api/v1/projects/proj-1/repos", json=_valid_repo_body())
    assert r.status_code == 403
    assert r.json() == {"detail": INSUFFICIENT, "code": "project_role.insufficient"}
    mock_project_svc_with_a_repo.add_repo.assert_not_called()


def test_store_read_failure_does_not_leak_governed_projects_in_the_list(
    client_factory_with_repositories, role_svc_with_both_read_pairs, mock_project_svc_with_a_repo
):
    """``GET /projects`` must not read an unreadable partition as an empty governed set, the maximally
    permissive answer; the handler renders the store error's row."""
    mock_project_svc_with_a_repo.list_projects.return_value = [_project("proj-1"), _project("proj-2")]
    _unverifiable(role_svc_with_both_read_pairs)
    client = client_factory_with_repositories(project_role=None)
    r = client.get("/api/v1/projects")
    assert r.status_code == 503
    assert r.json() == {
        "detail": "could not verify project ownership",
        "code": "project_role.ownership_unverified",
    }


def test_store_read_failure_does_not_leak_repositories_in_the_list(
    client_factory_with_repositories, role_svc_with_both_read_pairs, mock_project_svc_with_a_repo
):
    mock_project_svc_with_a_repo.list_repositories.return_value = [
        _repository(id="r-mine", project_id="proj-1"),
        _repository(id="r-theirs", project_id="proj-2"),
    ]
    mock_project_svc_with_a_repo.list_projects.return_value = [_project("proj-1"), _project("proj-2")]
    _unverifiable(role_svc_with_both_read_pairs)
    client = client_factory_with_repositories(project_role=None)
    r = client.get("/api/v1/repositories")
    assert r.status_code == 503
    assert r.json()["detail"] == "could not verify project ownership"


def test_store_read_failure_never_leaks_the_store_message(
    client_factory_with_repositories, mock_project_svc_with_a_repo
):
    """End to end through the real service: a live DynamoDB ``ClientError`` becomes the fixed 503 and
    its message (table arn, error code) never reaches the body."""
    from botocore.exceptions import ClientError

    import api.routes.projects as projects_module
    from core.state_store import StateStore
    from services.project_role_service import ProjectRoleService

    real = ProjectRoleService(table_name="")
    table = MagicMock()
    table.query.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "arn:aws:dynamodb:secret"}},
        "Query",
    )
    real._store = StateStore("projects", table=table)
    client = client_factory_with_repositories(project_role=None)
    projects_module._role_svc = real

    r = client.get("/api/v1/projects")
    assert r.status_code == 503
    assert r.json() == {
        "detail": "could not verify project ownership",
        "code": "project_role.ownership_unverified",
    }
    assert "dynamodb" not in r.text.lower() and "AccessDenied" not in r.text


def test_a_genuinely_ungoverned_project_still_works(
    client_factory_with_repositories, role_svc_with_both_read_pairs, mock_project_svc_with_a_repo
):
    """Failing closed on an unreadable partition must not break the readable-and-empty case."""
    role_svc_with_both_read_pairs.has_role_rows.return_value = False
    role_svc_with_both_read_pairs.list_all_strict.return_value = []
    mock_project_svc_with_a_repo.list_projects.return_value = [_project("proj-1")]
    client = client_factory_with_repositories(project_role=None)
    assert client.post("/api/v1/projects/proj-1/repos", json=_valid_repo_body()).status_code == 202
    assert client.get("/api/v1/projects/proj-1").status_code == 200
    assert [p["id"] for p in client.get("/api/v1/projects").json()] == ["proj-1"]


def test_list_projects_filters_to_visible_roles(
    client_factory_with_repositories, role_svc_with_both_read_pairs, mock_project_svc_with_a_repo
):
    """Both projects are governed; the caller holds a role on only one of them."""
    mock_project_svc_with_a_repo.list_projects.return_value = [_project("proj-1"), _project("proj-2")]
    role_svc_with_both_read_pairs.list_all_strict.return_value = [
        _governed_row(pid="proj-1"),
        _governed_row(pid="proj-2"),
    ]
    client = client_factory_with_repositories(roles={"proj-1": "viewer"})
    body = client.get("/api/v1/projects").json()
    assert [p["id"] for p in body] == ["proj-1"]


def test_list_projects_keeps_ungoverned_projects(
    client_factory_with_repositories, role_svc_with_both_read_pairs, mock_project_svc_with_a_repo
):
    """The ungoverned rule applies to the list too, or every pre-migration project would vanish from the
    UI for non-admins; proj-2 is governed by someone else."""
    mock_project_svc_with_a_repo.list_projects.return_value = [_project("proj-1"), _project("proj-2")]
    role_svc_with_both_read_pairs.list_all_strict.return_value = [_governed_row(pid="proj-2")]
    client = client_factory_with_repositories(project_role=None)
    body = client.get("/api/v1/projects").json()
    assert [p["id"] for p in body] == ["proj-1"]


def test_list_projects_reads_the_role_store_once(
    client_factory_with_repositories, role_svc_with_both_read_pairs, mock_project_svc_with_a_repo
):
    """One ``list_all_strict()`` for the whole page, never a ``has_role_rows`` per row."""
    mock_project_svc_with_a_repo.list_projects.return_value = [_project(f"proj-{i}") for i in range(5)]
    role_svc_with_both_read_pairs.list_all_strict.return_value = []
    client = client_factory_with_repositories(project_role=None)
    assert client.get("/api/v1/projects").status_code == 200
    assert role_svc_with_both_read_pairs.list_all_strict.call_count == 1
    role_svc_with_both_read_pairs.has_role_rows.assert_not_called()


def test_list_repositories_filters_by_parent_project_role(
    client_factory_with_repositories, role_svc_with_both_read_pairs, mock_project_svc_with_a_repo
):
    """Repositories carry no role of their own; authority is inherited from the parent project."""
    mock_project_svc_with_a_repo.list_repositories.return_value = [
        _repository(id="r-mine", project_id="proj-1"),
        _repository(id="r-theirs", project_id="proj-2"),
    ]
    mock_project_svc_with_a_repo.list_projects.return_value = [_project("proj-1"), _project("proj-2")]
    role_svc_with_both_read_pairs.list_all_strict.return_value = [
        _governed_row(pid="proj-1"),
        _governed_row(pid="proj-2"),
    ]
    client = client_factory_with_repositories(roles={"proj-1": "viewer"})
    body = client.get("/api/v1/repositories").json()
    assert [r["id"] for r in body] == ["r-mine"]


def test_list_repositories_reads_the_role_store_once(
    client_factory_with_repositories, role_svc_with_both_read_pairs, mock_project_svc_with_a_repo
):
    mock_project_svc_with_a_repo.list_repositories.return_value = [
        _repository(id=f"r-{i}", project_id=f"proj-{i}") for i in range(5)
    ]
    mock_project_svc_with_a_repo.list_projects.return_value = [_project(f"proj-{i}") for i in range(5)]
    role_svc_with_both_read_pairs.list_all_strict.return_value = []
    client = client_factory_with_repositories(project_role=None)
    assert client.get("/api/v1/repositories").status_code == 200
    assert role_svc_with_both_read_pairs.list_all_strict.call_count == 1
    role_svc_with_both_read_pairs.has_role_rows.assert_not_called()


def test_create_project_has_no_project_gate(
    client_factory_with_repositories, role_svc_with_both_read_pairs, mock_project_svc_with_a_repo
):
    """There is no project to hold a role on yet; tenant membership is the only gate."""
    client = client_factory_with_repositories(project_role=None)
    r = client.post(
        "/api/v1/projects",
        json={"name": "P", "connection_id": "conn-1", "tenant_id": "default"},
    )
    assert r.status_code == 201
    mock_project_svc_with_a_repo.create_project.assert_called_once()


@pytest.mark.parametrize("method,path,body,required_role", _GATED)
def test_admin_sees_and_may_everything(
    method, path, body, required_role, client_factory_with_repositories, role_svc_with_both_read_pairs
):
    """A global admin short-circuits ``may()``, so the role partition is never read."""
    _governed(role_svc_with_both_read_pairs, "proj-1")
    client = client_factory_with_repositories(platform_role="admin")
    r = _call(client, method, path, body)
    assert r.status_code < 400, r.text
    role_svc_with_both_read_pairs.has_role_rows.assert_not_called()


def test_admin_sees_every_project_in_both_lists(
    client_factory_with_repositories, role_svc_with_both_read_pairs, mock_project_svc_with_a_repo
):
    """The same short-circuit on the list side: neither list touches the strict whole-partition read,
    so an admin never inherits a 503 from an unreadable role partition."""
    mock_project_svc_with_a_repo.list_projects.return_value = [_project("proj-1"), _project("proj-2")]
    mock_project_svc_with_a_repo.list_repositories.return_value = [
        _repository(id="r-1", project_id="proj-1"),
        _repository(id="r-2", project_id="proj-2"),
    ]
    role_svc_with_both_read_pairs.list_all_strict.return_value = [
        _governed_row(pid="proj-1"),
        _governed_row(pid="proj-2"),
    ]
    client = client_factory_with_repositories(
        platform_role="admin", tenant_ctx=_tenant_context(is_global=True)
    )
    assert {p["id"] for p in client.get("/api/v1/projects").json()} == {"proj-1", "proj-2"}
    assert {r["id"] for r in client.get("/api/v1/repositories").json()} == {"r-1", "r-2"}
    role_svc_with_both_read_pairs.list_all_strict.assert_not_called()


def test_detail_reports_the_callers_effective_role(
    client_factory_with_repositories, role_svc_with_both_read_pairs
):
    _governed(role_svc_with_both_read_pairs, "proj-1")
    client = client_factory_with_repositories(project_role="maintainer")
    body = client.get("/api/v1/projects/proj-1").json()
    assert body["effective_role"] == "maintainer"
    assert body["ungoverned"] is False


def test_detail_reports_a_group_derived_owner_as_owner(
    client_factory_with_repositories, role_svc_with_both_read_pairs
):
    """The caller has no direct row; their owner comes from a group the resolver matched."""
    _governed(role_svc_with_both_read_pairs, "proj-1")
    client = client_factory_with_repositories(roles={"proj-1": "owner"})
    body = client.get("/api/v1/projects/proj-1").json()
    assert body["effective_role"] == "owner"


def test_detail_reports_a_global_admin_as_owner(
    client_factory_with_repositories, role_svc_with_both_read_pairs
):
    _governed(role_svc_with_both_read_pairs, "proj-1")
    client = client_factory_with_repositories(platform_role="admin")
    body = client.get("/api/v1/projects/proj-1").json()
    assert body["effective_role"] == "owner"


def test_detail_reports_no_role_as_null_on_an_ungoverned_project(
    client_factory_with_repositories, role_svc_with_both_read_pairs
):
    """The caller holds nothing (``effective_role`` null) but the project is ungoverned, so maintainer
    verbs will succeed; that is what the second field says."""
    role_svc_with_both_read_pairs.has_role_rows.return_value = False
    client = client_factory_with_repositories(project_role=None)
    body = client.get("/api/v1/projects/proj-1").json()
    assert body["effective_role"] is None
    assert body["ungoverned"] is True


def test_the_hint_agrees_with_what_the_gates_actually_allow(
    client_factory_with_repositories, role_svc_with_both_read_pairs, mock_project_svc_with_a_repo
):
    """The UI must never show an affordance the backend then refuses: the maintainer verb succeeds and
    the owner verb is refused, exactly as the hints advertise."""
    role_svc_with_both_read_pairs.has_role_rows.return_value = False
    client = client_factory_with_repositories(project_role=None)
    body = client.get("/api/v1/projects/proj-1").json()
    assert body["effective_role"] is None and body["ungoverned"] is True
    assert client.post("/api/v1/projects/proj-1/repos", json=_valid_repo_body()).status_code == 202
    assert client.delete("/api/v1/projects/proj-1").status_code == 403


def test_the_hint_never_widens_the_gate(
    client_factory_with_repositories, role_svc_with_both_read_pairs, mock_project_svc_with_a_repo
):
    """A hint is serialized output, never an input to a decision."""
    _governed(role_svc_with_both_read_pairs, "proj-1")
    client = client_factory_with_repositories(project_role="viewer")
    assert client.get("/api/v1/projects/proj-1").json()["effective_role"] == "viewer"
    assert client.delete("/api/v1/projects/proj-1").status_code == 403
    mock_project_svc_with_a_repo.delete_project.assert_not_called()


def test_the_hint_costs_no_extra_role_store_read(
    client_factory_with_repositories, role_svc_with_both_read_pairs
):
    """Derived from the already-resolved context, not a second store read."""
    _governed(role_svc_with_both_read_pairs, "proj-1")
    client = client_factory_with_repositories(project_role="owner")
    assert client.get("/api/v1/projects/proj-1").status_code == 200
    role_svc_with_both_read_pairs.has_role_rows.assert_not_called()
    role_svc_with_both_read_pairs.list_all_strict.assert_not_called()


def test_platform_viewer_still_403s_before_any_project_logic(
    client_factory_with_repositories, role_svc_with_both_read_pairs
):
    client = client_factory_with_repositories(platform_role="viewer", project_role="owner")
    assert client.delete("/api/v1/projects/proj-1").status_code == 403
    role_svc_with_both_read_pairs.has_role_rows.assert_not_called()


_CALLER_OID = "operator-oid"


def _own_row(pid="proj-1", role="owner"):
    """A role row for the caller themselves, as ``_claims_for`` mints the operator's oid."""
    return _governed_row(pid=pid, principal=_CALLER_OID, role=role)


def _stale_cache_client(client_factory, role_svc, *, rows, pid="proj-1"):
    """A client whose cached context is empty while the store holds ``rows``: the second ECS task
    mid-window. ``has_role_rows`` is live, so the project reads as governed."""
    role_svc.has_role_rows.side_effect = lambda p: p == pid
    role_svc.list_for_project_strict.side_effect = lambda p: [r for r in rows if r.project_id == p]
    role_svc.list_all_strict.return_value = list(rows)
    return client_factory(project_role=None)


def test_a_stale_cache_does_not_403_the_creator_of_their_own_project(
    client_factory_with_repositories, role_svc_with_both_read_pairs, mock_project_svc_with_a_repo
):
    """The creator holds owner in the store while this task's cache predates it."""
    client = _stale_cache_client(
        client_factory_with_repositories, role_svc_with_both_read_pairs, rows=[_own_row()]
    )
    assert client.delete("/api/v1/projects/proj-1").status_code == 204
    mock_project_svc_with_a_repo.delete_project.assert_called_once_with("proj-1")


def test_a_stale_cache_does_not_hide_the_promote_or_roster_gates(
    client_factory_with_repositories, role_svc_with_both_read_pairs
):
    """The strict gate takes the same fresh confirmation."""
    client = _stale_cache_client(
        client_factory_with_repositories, role_svc_with_both_read_pairs, rows=[_own_row()]
    )
    assert client.get("/api/v1/projects/proj-1/roles").status_code == 200


def test_the_detail_hint_reports_the_role_the_fresh_read_found(
    client_factory_with_repositories, role_svc_with_both_read_pairs
):
    client = _stale_cache_client(
        client_factory_with_repositories, role_svc_with_both_read_pairs, rows=[_own_row()]
    )
    body = client.get("/api/v1/projects/proj-1").json()
    assert body["effective_role"] == "owner"
    assert body["ungoverned"] is False


def test_the_list_and_the_detail_agree_during_the_window(
    client_factory_with_repositories, role_svc_with_both_read_pairs, mock_project_svc_with_a_repo
):
    mock_project_svc_with_a_repo.list_projects.return_value = [_project("proj-1")]
    client = _stale_cache_client(
        client_factory_with_repositories, role_svc_with_both_read_pairs, rows=[_own_row()]
    )
    assert [p["id"] for p in client.get("/api/v1/projects").json()] == ["proj-1"]
    assert client.get("/api/v1/projects/proj-1").json()["effective_role"] == "owner"


def test_a_stale_cache_never_widens_beyond_what_the_store_says(
    client_factory_with_repositories, role_svc_with_both_read_pairs
):
    """The refresh is additive, not permissive: a maintainer row stays a maintainer."""
    client = _stale_cache_client(
        client_factory_with_repositories, role_svc_with_both_read_pairs, rows=[_own_row(role="maintainer")]
    )
    assert client.post("/api/v1/projects/proj-1/repos", json=_valid_repo_body()).status_code == 202
    r = client.delete("/api/v1/projects/proj-1")
    assert r.status_code == 403
    assert r.json() == {"detail": INSUFFICIENT, "code": "project_role.insufficient"}


def test_a_caller_with_no_row_at_all_is_still_refused(
    client_factory_with_repositories, role_svc_with_both_read_pairs, mock_project_svc_with_a_repo
):
    """The fresh read is a confirmation, not an escape hatch."""
    client = _stale_cache_client(
        client_factory_with_repositories, role_svc_with_both_read_pairs, rows=[_governed_row()]
    )
    assert client.delete("/api/v1/projects/proj-1").status_code == 403
    mock_project_svc_with_a_repo.delete_project.assert_not_called()


def test_the_confirmation_read_only_happens_on_the_deny_path(
    client_factory_with_repositories, role_svc_with_both_read_pairs
):
    _governed(role_svc_with_both_read_pairs, "proj-1")
    client = client_factory_with_repositories(project_role="owner")
    assert client.get("/api/v1/projects/proj-1").status_code == 200
    role_svc_with_both_read_pairs.list_for_project_strict.assert_not_called()


def test_an_unreadable_partition_leaves_the_refusal_standing(
    client_factory_with_repositories, role_svc_with_both_read_pairs
):
    """If the confirmation read itself faults, the original 403 stands and never becomes a 500."""
    from services.project_role_service import ProjectRoleError

    role_svc_with_both_read_pairs.has_role_rows.side_effect = lambda p: True
    role_svc_with_both_read_pairs.list_for_project_strict.side_effect = ProjectRoleError(
        "boom", kind="ownership_unverified"
    )
    client = client_factory_with_repositories(project_role=None)
    r = client.delete("/api/v1/projects/proj-1")
    assert r.status_code == 403
    assert r.json() == {"detail": INSUFFICIENT, "code": "project_role.insufficient"}


def test_deleting_a_project_deletes_its_role_rows(
    client_factory_with_repositories, role_svc_with_both_read_pairs, mock_project_svc_with_a_repo
):
    _governed(role_svc_with_both_read_pairs, "proj-1")
    client = client_factory_with_repositories(project_role="owner")
    assert client.delete("/api/v1/projects/proj-1").status_code == 204
    role_svc_with_both_read_pairs.revoke_all.assert_called_once_with("proj-1")


def test_the_role_rows_are_deleted_only_AFTER_the_project_is_gone(
    client_factory_with_repositories, role_svc_with_both_read_pairs, mock_project_svc_with_a_repo
):
    """Stripping the owners of a project that then fails to delete would leave a live project nobody can
    administer."""
    order = []
    mock_project_svc_with_a_repo.delete_project.side_effect = lambda pid: order.append("project")
    role_svc_with_both_read_pairs.revoke_all.side_effect = lambda pid: order.append("roles") or 1
    _governed(role_svc_with_both_read_pairs, "proj-1")
    client = client_factory_with_repositories(project_role="owner")
    assert client.delete("/api/v1/projects/proj-1").status_code == 204
    assert order == ["project", "roles"]


def test_a_refused_project_delete_keeps_its_role_rows(
    client_factory_with_repositories, role_svc_with_both_read_pairs, mock_project_svc_with_a_repo
):
    """A 409 (repos still exist) is not a delete."""
    from services.project_errors import ProjectError

    mock_project_svc_with_a_repo.delete_project.side_effect = ProjectError("nope", kind="has_repositories")
    _governed(role_svc_with_both_read_pairs, "proj-1")
    client = client_factory_with_repositories(project_role="owner")
    assert client.delete("/api/v1/projects/proj-1").status_code == 409
    role_svc_with_both_read_pairs.revoke_all.assert_not_called()


def test_a_failed_role_cleanup_does_not_fail_the_completed_delete(
    client_factory_with_repositories, role_svc_with_both_read_pairs, mock_project_svc_with_a_repo
):
    """The project is already gone, so raising would 500 a delete that succeeded."""
    role_svc_with_both_read_pairs.revoke_all.side_effect = RuntimeError("ddb down")
    _governed(role_svc_with_both_read_pairs, "proj-1")
    client = client_factory_with_repositories(project_role="owner")
    assert client.delete("/api/v1/projects/proj-1").status_code == 204
    mock_project_svc_with_a_repo.delete_project.assert_called_once_with("proj-1")


NO_CANDIDATE = "no prod candidate to promote"
_PROMOTE = "/api/v1/projects/proj-1/repos/r-1/promote"


def test_no_prod_candidate_is_409_not_500(
    client_factory_with_repositories, role_svc_with_both_read_pairs, mock_project_svc_with_a_repo
):
    """Nothing has merged to ``main`` since the last release: an ordinary state, a 409 the UI renders."""
    from services.project_errors import ProjectError

    mock_project_svc_with_a_repo.promote_repo.side_effect = ProjectError(
        "no prod candidate to promote", kind="no_prod_candidate"
    )
    _governed(role_svc_with_both_read_pairs, "proj-1")
    client = client_factory_with_repositories(project_role="owner")
    r = client.post(_PROMOTE)
    assert r.status_code == 409, r.text
    assert r.json() == {"detail": NO_CANDIDATE, "code": "project.no_prod_candidate"}


def test_no_prod_candidate_detail_is_the_fixed_literal_not_the_error_message(
    client_factory_with_repositories, role_svc_with_both_read_pairs, mock_project_svc_with_a_repo
):
    """The table row's fixed detail, never the service's message."""
    from services.project_errors import ProjectError

    mock_project_svc_with_a_repo.promote_repo.side_effect = ProjectError(
        "ddb said: table agp-repos throttled on sk=REPO#r-1", kind="no_prod_candidate"
    )
    _governed(role_svc_with_both_read_pairs, "proj-1")
    client = client_factory_with_repositories(project_role="owner")
    r = client.post(_PROMOTE)
    assert r.status_code == 409
    assert r.json()["detail"] == NO_CANDIDATE
    assert "throttled" not in r.text


def test_a_non_owner_still_cannot_promote(
    client_factory_with_repositories, role_svc_with_both_read_pairs, mock_project_svc_with_a_repo
):
    """Promote's strict owner gate is untouched: a maintainer is refused before the deploy."""
    _governed(role_svc_with_both_read_pairs, "proj-1")
    client = client_factory_with_repositories(project_role="maintainer")
    r = client.post(_PROMOTE)
    assert r.status_code == 403
    assert r.json() == {"detail": INSUFFICIENT, "code": "project_role.insufficient"}
    mock_project_svc_with_a_repo.promote_repo.assert_not_called()
