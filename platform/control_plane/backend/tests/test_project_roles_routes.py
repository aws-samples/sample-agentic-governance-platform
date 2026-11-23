"""Per-project role routes.

Mirrors ``test_projects_routes.py``: the module reset, the entra env fixture, fake resolvers on
the ``users`` module globals (the one resolver singletons), and the services as ``MagicMock``s on the
``projects`` module globals. No ``dependency_overrides``: the real ``require_role`` /
``current_principal`` / ``get_tenant_ctx`` / ``get_project_ctx`` chain runs against a mocked
``verify_entra_token`` and mocked services.

Pinned here: the two gates compose in order (``_load_visible_project`` first, so a foreign tenant 404s
before any role logic, then ``may()``); viewer reads the roster, owner changes it, maintainer cannot; a
global platform admin bypasses the project role; ``granted_by`` comes from the principal, never the
body; every ``ProjectRoleError`` kind renders its table row through the one handler; every successful
write invalidates the resolver's cache; ``create_project`` bootstraps the creator as owner keyed on
``oid``, and a grant failure never fails the create.
"""

from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from models.project import Project, ProjectDetail
from models.project_role import ProjectRole, ProjectRoleRecord

pytestmark = pytest.mark.usefixtures("reset_modules")

FIXED_TS = "2026-07-27T00:00:00+00:00"
INSUFFICIENT = "insufficient project role"


def _row(pid="proj-1", principal="oid-1", role="owner"):
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


def _tenant_context(*, is_global=False, tenant_ids=("default",)):
    from services.tenant_resolver import TenantContext

    return TenantContext(is_global=is_global, tenant_ids=frozenset(tenant_ids), tenants=())


def _project_context(*, is_global=False, role=None, project_id="proj-1"):
    from services.project_resolver import ProjectContext

    return ProjectContext(is_global=is_global, roles={} if role is None else {project_id: role})


class _FakeTenantResolver:
    def __init__(self, ctx):
        self._ctx = ctx

    async def resolve(self, principal):
        return self._ctx


class _FakeProjectResolver:
    """Async ``resolve`` stub, a spy-able ``invalidate``, and the real ``refresh_project`` semantics: the
    gates call it on every deny, so it reads the same ``_role_svc`` the test seeds through the real
    ``context_from_rows`` fold rather than letting the route swallow an ``AttributeError``."""

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
def role_svc_strict_roster_follows_seed():
    """The ``ProjectRoleService`` MagicMock on the projects module global. The list route reads the
    strict roster, so ``list_for_project_strict`` is derived from the degrading seed: a test that sets
    rows gets them from either read, and a regression back to the degrading read stays visible as a
    call on ``list_for_project``."""
    import api.routes.projects as projects_module

    svc = MagicMock()
    svc.list_for_project.return_value = []
    svc.list_for_project_strict.side_effect = lambda pid: svc.list_for_project(pid)
    projects_module._role_svc = svc
    return svc


@pytest.fixture
def mock_project_svc():
    """The ``ProjectService`` MagicMock on the projects module global, defaulting to a visible project so
    the tenant gate passes and the role gate is what's under test."""
    import api.routes.projects as projects_module

    svc = MagicMock()
    svc.get_project.return_value = _detail()
    svc.create_project.return_value = _project()
    projects_module._svc = svc
    return svc


def _build_client(*, platform_role, project_ctx, tenant_ctx, role_svc, project_svc):
    import api.routes.project_repos as project_repos_module
    import api.routes.projects as projects_module
    import api.routes.tenants as tenants_module
    import api.routes.users as users_module
    from core.errors import install_error_handlers

    users_module._tenant_resolver = _FakeTenantResolver(tenant_ctx)
    users_module._project_resolver = _FakeProjectResolver(project_ctx)
    tenants_module._svc = _FakeTenantService({"default", "ten-1", "ten-2"})

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(projects_module.router, prefix="/api/v1")
    app.include_router(project_repos_module.router, prefix="/api/v1")
    client = TestClient(app, headers={"Authorization": "Bearer fake-token"})
    patcher = patch("core.security_entra.verify_entra_token", return_value=_claims_for(platform_role))
    patcher.start()
    client._entra_patcher = patcher
    client._project_resolver = users_module._project_resolver
    return client


@pytest.fixture
def make_client(entra_settings, role_svc_strict_roster_follows_seed, mock_project_svc):
    """Factory the per-role client fixtures share; stops the entra patch on teardown."""
    built = []

    def _make(*, platform_role="operator", project_ctx=None, tenant_ctx=None):
        client = _build_client(
            platform_role=platform_role,
            project_ctx=project_ctx if project_ctx is not None else _project_context(),
            tenant_ctx=tenant_ctx if tenant_ctx is not None else _tenant_context(),
            role_svc=role_svc_strict_roster_follows_seed,
            project_svc=mock_project_svc,
        )
        built.append(client)
        return client

    yield _make
    for client in built:
        client._entra_patcher.stop()


@pytest.fixture
def client_as_owner(make_client):
    return make_client(project_ctx=_project_context(role=ProjectRole.OWNER))


@pytest.fixture
def client_as_maintainer(make_client):
    return make_client(project_ctx=_project_context(role=ProjectRole.MAINTAINER))


@pytest.fixture
def client_as_viewer(make_client):
    return make_client(project_ctx=_project_context(role=ProjectRole.VIEWER))


@pytest.fixture
def client_as_admin(make_client):
    """A global platform admin: ``may()`` short-circuits True regardless of the roles map."""
    return make_client(platform_role="admin", project_ctx=_project_context(is_global=True))


@pytest.fixture
def client_as_operator(make_client):
    """A plain platform operator holding no project role (the create-project caller)."""
    return make_client(project_ctx=_project_context())


@pytest.fixture
def client_foreign_tenant(make_client, mock_project_svc):
    """Project-role owner but a member of a different tenant: the tenant gate runs first (404) and the
    role never gets a say."""
    mock_project_svc.get_project.return_value = _detail(_project(tenant_id="ten-2"))
    return make_client(
        project_ctx=_project_context(role=ProjectRole.OWNER),
        tenant_ctx=_tenant_context(tenant_ids=["ten-1"]),
    )


def test_owner_can_list_and_grant(client_as_owner, role_svc_strict_roster_follows_seed):
    """A forged ``granted_by`` in the body is ignored: ``ProjectRoleCreate`` has no such field and the
    route sources it from the authenticated principal's oid."""
    role_svc_strict_roster_follows_seed.list_for_project.return_value = [_row()]
    r = client_as_owner.get("/api/v1/projects/proj-1/roles")
    assert r.status_code == 200
    assert r.json()[0]["principal_id"] == "oid-1"

    role_svc_strict_roster_follows_seed.grant.return_value = _row(principal="oid-2", role="maintainer")
    r = client_as_owner.post(
        "/api/v1/projects/proj-1/roles",
        json={
            "principal_id": "oid-2",
            "principal_type": "user",
            "principal_display": "Sam",
            "role": "maintainer",
            "granted_by": "oid-FORGED",
        },
    )
    assert r.status_code == 201
    assert role_svc_strict_roster_follows_seed.grant.call_args.kwargs["granted_by"] == "operator-oid"


def test_viewer_can_list_but_not_grant(client_as_viewer, role_svc_strict_roster_follows_seed):
    """The guard runs before the write."""
    role_svc_strict_roster_follows_seed.list_for_project.return_value = [_row()]
    assert client_as_viewer.get("/api/v1/projects/proj-1/roles").status_code == 200

    r = client_as_viewer.post(
        "/api/v1/projects/proj-1/roles",
        json={"principal_id": "oid-9", "principal_type": "user", "role": "viewer"},
    )
    assert r.status_code == 403
    assert r.json() == {"detail": INSUFFICIENT, "code": "project_role.insufficient"}
    role_svc_strict_roster_follows_seed.grant.assert_not_called()


def test_maintainer_cannot_manage_roles(client_as_maintainer, role_svc_strict_roster_follows_seed):
    r = client_as_maintainer.delete("/api/v1/projects/proj-1/roles/oid-1")
    assert r.status_code == 403
    role_svc_strict_roster_follows_seed.revoke.assert_not_called()


def test_maintainer_can_still_read_the_roster(client_as_maintainer, role_svc_strict_roster_follows_seed):
    """``may()`` is a threshold, not an equality."""
    role_svc_strict_roster_follows_seed.list_for_project.return_value = [_row()]
    assert client_as_maintainer.get("/api/v1/projects/proj-1/roles").status_code == 200


def test_no_project_role_cannot_even_list(client_as_operator, role_svc_strict_roster_follows_seed):
    """Fail closed: a platform operator with zero role rows on the project sees a 403, not an empty
    roster, and the only read on this path is the gate's own stale-cache confirmation."""
    r = client_as_operator.get("/api/v1/projects/proj-1/roles")
    assert r.status_code == 403
    assert r.json() == {"detail": INSUFFICIENT, "code": "project_role.insufficient"}
    assert not isinstance(r.json(), list)
    assert role_svc_strict_roster_follows_seed.list_for_project_strict.call_args_list == [call("proj-1")]


def test_platform_viewer_still_403s_before_any_project_logic(
    make_client, role_svc_strict_roster_follows_seed
):
    """The project role is checked in addition to the platform role, never instead: a Platform.Viewer
    is refused by ``require_role(OPERATOR)`` even holding project owner."""
    client = make_client(platform_role="viewer", project_ctx=_project_context(role=ProjectRole.OWNER))
    assert client.get("/api/v1/projects/proj-1/roles").status_code == 403
    role_svc_strict_roster_follows_seed.list_for_project.assert_not_called()


def test_foreign_tenant_404s_before_any_role_logic(
    client_foreign_tenant, role_svc_strict_roster_follows_seed
):
    r = client_foreign_tenant.get("/api/v1/projects/proj-1/roles")
    assert r.status_code == 404
    role_svc_strict_roster_follows_seed.list_for_project.assert_not_called()


def test_foreign_tenant_404_is_byte_identical_to_a_missing_project(client_foreign_tenant, mock_project_svc):
    """A foreign tenant's project must look absent."""
    foreign = client_foreign_tenant.get("/api/v1/projects/proj-1/roles")
    mock_project_svc.get_project.return_value = None
    missing = client_foreign_tenant.get("/api/v1/projects/truly-missing/roles")
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json() == {"detail": "Project not found", "code": "project.not_found"}


def test_owner_of_projA_cannot_manage_projB(
    make_client, role_svc_strict_roster_follows_seed, mock_project_svc
):
    """The gate is scoped per project, not "holds the role somewhere": proj-2 is in the same tenant, so
    ``may(pctx, id, ...)`` is the only thing between the caller and it."""
    mock_project_svc.get_project.return_value = _detail(_project(id="proj-2"))
    client = make_client(project_ctx=_project_context(role=ProjectRole.OWNER, project_id="proj-1"))
    role_svc_strict_roster_follows_seed.grant.return_value = _row(pid="proj-2")
    r = client.post(
        "/api/v1/projects/proj-2/roles",
        json={"principal_id": "attacker", "principal_type": "user", "role": "owner"},
    )
    assert r.status_code == 403
    assert r.json() == {"detail": INSUFFICIENT, "code": "project_role.insufficient"}
    role_svc_strict_roster_follows_seed.grant.assert_not_called()


def test_owner_of_projA_cannot_even_read_projB_roster(
    make_client, role_svc_strict_roster_follows_seed, mock_project_svc
):
    """The same scoping on the read verb; only proj-2's own gate-confirmation read happens."""
    mock_project_svc.get_project.return_value = _detail(_project(id="proj-2"))
    client = make_client(project_ctx=_project_context(role=ProjectRole.OWNER, project_id="proj-1"))
    r = client.get("/api/v1/projects/proj-2/roles")
    assert r.status_code == 403
    assert role_svc_strict_roster_follows_seed.list_for_project_strict.call_args_list == [call("proj-2")]


def test_admin_bypasses_project_role(client_as_admin, role_svc_strict_roster_follows_seed):
    role_svc_strict_roster_follows_seed.grant.return_value = _row()
    r = client_as_admin.post(
        "/api/v1/projects/proj-1/roles",
        json={"principal_id": "oid-3", "principal_type": "group", "role": "owner"},
    )
    assert r.status_code == 201


def test_update_role_uses_the_path_principal_not_the_body(
    client_as_owner, role_svc_strict_roster_follows_seed
):
    """The store's ``grant`` is an upsert keyed on (project, principal), so a PUT is a re-grant and the
    path id wins over a mismatched body; the grantor is the authenticated principal."""
    role_svc_strict_roster_follows_seed.grant.return_value = _row(principal="oid-1", role="viewer")
    r = client_as_owner.put(
        "/api/v1/projects/proj-1/roles/oid-1",
        json={
            "principal_id": "oid-EVIL",
            "principal_type": "user",
            "role": "viewer",
            "granted_by": "oid-FORGED",
        },
    )
    assert r.status_code == 200
    assert role_svc_strict_roster_follows_seed.grant.call_args.args[1].principal_id == "oid-1"
    assert role_svc_strict_roster_follows_seed.grant.call_args.kwargs["granted_by"] == "operator-oid"


def test_last_owner_downgrade_via_put_maps_to_409(client_as_owner, role_svc_strict_roster_follows_seed):
    """The PUT verb is an upsert, so it can strand a project with zero owners exactly like a revoke."""
    from services.project_role_service import ProjectRoleError

    role_svc_strict_roster_follows_seed.grant.side_effect = ProjectRoleError(
        "secret internals", kind="last_owner"
    )
    r = client_as_owner.put(
        "/api/v1/projects/proj-1/roles/oid-1",
        json={"principal_id": "oid-1", "principal_type": "user", "role": "viewer"},
    )
    assert r.status_code == 409
    assert r.json() == {"detail": "project must keep at least one owner", "code": "project_role.last_owner"}
    assert "secret internals" not in r.text
    client_as_owner._project_resolver.invalidate.assert_not_called()


def test_last_owner_downgrade_via_post_maps_to_409(client_as_owner, role_svc_strict_roster_follows_seed):
    """POST hits the same upsert as PUT, so a body naming the sole owner with a lesser role is the same
    zero-owner lockout."""
    from services.project_role_service import ProjectRoleError

    role_svc_strict_roster_follows_seed.grant.side_effect = ProjectRoleError(
        "secret internals", kind="last_owner"
    )
    r = client_as_owner.post(
        "/api/v1/projects/proj-1/roles",
        json={"principal_id": "oid-1", "principal_type": "user", "role": "viewer"},
    )
    assert r.status_code == 409
    assert r.json()["detail"] == "project must keep at least one owner"
    assert "secret internals" not in r.text
    client_as_owner._project_resolver.invalidate.assert_not_called()


@pytest.mark.parametrize("verb", ["post", "put"])
def test_unverifiable_ownership_maps_to_503_on_both_upsert_verbs(
    client_as_owner, role_svc_strict_roster_follows_seed, verb
):
    """The store refuses a role change it cannot prove is safe (an unreadable role partition): a
    transient store fault, 503 with the fixed detail, never the store's message."""
    from services.project_role_service import ProjectRoleError

    role_svc_strict_roster_follows_seed.grant.side_effect = ProjectRoleError(
        "ProvisionedThroughputExceededException", kind="ownership_unverified"
    )
    body = {"principal_id": "oid-1", "principal_type": "user", "role": "viewer"}
    url = "/api/v1/projects/proj-1/roles" if verb == "post" else "/api/v1/projects/proj-1/roles/oid-1"
    r = getattr(client_as_owner, verb)(url, json=body)
    assert r.status_code == 503
    assert r.json() == {
        "detail": "could not verify project ownership",
        "code": "project_role.ownership_unverified",
    }
    assert "ProvisionedThroughputExceededException" not in r.text
    client_as_owner._project_resolver.invalidate.assert_not_called()


def test_unreadable_roster_is_503_not_an_empty_list(client_as_owner, role_svc_strict_roster_follows_seed):
    """The console uses the roster's emptiness as a decision (its Grant is an add only because the
    roster says who already holds a role), so a store fault must fail the read, never come back empty."""
    from services.project_role_service import ProjectRoleError

    role_svc_strict_roster_follows_seed.list_for_project_strict.side_effect = ProjectRoleError(
        "ProvisionedThroughputExceededException", kind="ownership_unverified"
    )
    r = client_as_owner.get("/api/v1/projects/proj-1/roles")
    assert r.status_code == 503, "a store fault must never read as an empty roster"
    assert r.json()["detail"] == "could not verify project ownership"
    assert "ProvisionedThroughputExceededException" not in r.text


def test_the_roster_route_reads_the_strict_loader(client_as_owner, role_svc_strict_roster_follows_seed):
    """The degrading ``list_for_project`` swallows a ``ClientError`` into ``[]``, the false 200 above."""
    role_svc_strict_roster_follows_seed.list_for_project.return_value = [_row()]
    assert client_as_owner.get("/api/v1/projects/proj-1/roles").status_code == 200
    role_svc_strict_roster_follows_seed.list_for_project_strict.assert_called_once_with("proj-1")


def test_a_genuinely_empty_roster_is_still_200(client_as_owner, role_svc_strict_roster_follows_seed):
    role_svc_strict_roster_follows_seed.list_for_project.return_value = []
    r = client_as_owner.get("/api/v1/projects/proj-1/roles")
    assert r.status_code == 200
    assert r.json() == []


def test_revoke_returns_204(client_as_owner, role_svc_strict_roster_follows_seed):
    role_svc_strict_roster_follows_seed.revoke.return_value = None
    r = client_as_owner.delete("/api/v1/projects/proj-1/roles/oid-1")
    assert r.status_code == 204
    role_svc_strict_roster_follows_seed.revoke.assert_called_once_with("proj-1", "oid-1")


def test_last_owner_revoke_maps_to_409(client_as_owner, role_svc_strict_roster_follows_seed):
    from services.project_role_service import ProjectRoleError

    role_svc_strict_roster_follows_seed.revoke.side_effect = ProjectRoleError("nope", kind="last_owner")
    r = client_as_owner.delete("/api/v1/projects/proj-1/roles/oid-1")
    assert r.status_code == 409
    assert r.json()["detail"] == "project must keep at least one owner"


def test_revoke_unknown_grant_maps_to_404(client_as_owner, role_svc_strict_roster_follows_seed):
    from services.project_role_service import ProjectRoleError

    role_svc_strict_roster_follows_seed.revoke.side_effect = ProjectRoleError(
        "secret internals", kind="not_found"
    )
    r = client_as_owner.delete("/api/v1/projects/proj-1/roles/oid-nope")
    assert r.status_code == 404
    assert r.json() == {"detail": "project role not found", "code": "project_role.not_found"}
    assert "secret internals" not in r.text


def test_update_unknown_grant_maps_to_404(client_as_owner, role_svc_strict_roster_follows_seed):
    from services.project_role_service import ProjectRoleError

    role_svc_strict_roster_follows_seed.grant.side_effect = ProjectRoleError(
        "secret internals", kind="not_found"
    )
    r = client_as_owner.put(
        "/api/v1/projects/proj-1/roles/oid-nope",
        json={"principal_id": "oid-nope", "principal_type": "user", "role": "viewer"},
    )
    assert r.status_code == 404
    assert r.json()["detail"] == "project role not found"
    client_as_owner._project_resolver.invalidate.assert_not_called()


def test_grant_validation_error_maps_to_400_with_a_fixed_detail(
    client_as_owner, role_svc_strict_roster_follows_seed
):
    """A bad role name and a ``#`` in either key component both surface as the same fixed 400."""
    from services.project_role_service import ProjectRoleError

    role_svc_strict_roster_follows_seed.grant.side_effect = ProjectRoleError(
        "secret internals", kind="validation"
    )
    r = client_as_owner.post(
        "/api/v1/projects/proj-1/roles",
        json={"principal_id": "oid#evil", "principal_type": "user", "role": "owner"},
    )
    assert r.status_code == 400
    assert r.json() == {"detail": "invalid project role", "code": "project_role.validation"}
    assert "secret internals" not in r.text


@pytest.mark.parametrize(
    "verb,url",
    [
        ("get", "/api/v1/projects/proj-1/roles"),
        ("put", "/api/v1/projects/proj-1/roles/oid-1"),
        ("delete", "/api/v1/projects/proj-1/roles/oid-1"),
    ],
)
def test_validation_error_maps_to_400_on_the_other_verbs(
    client_as_owner, role_svc_strict_roster_follows_seed, verb, url
):
    from services.project_role_service import ProjectRoleError

    boom = ProjectRoleError("project_id must not contain '#'", kind="validation")
    role_svc_strict_roster_follows_seed.list_for_project_strict.side_effect = boom
    role_svc_strict_roster_follows_seed.grant.side_effect = boom
    role_svc_strict_roster_follows_seed.revoke.side_effect = boom
    body = {"principal_id": "oid-1", "principal_type": "user", "role": "viewer"}
    r = getattr(client_as_owner, verb)(url, **({"json": body} if verb == "put" else {}))
    assert r.status_code == 400
    assert r.json()["detail"] == "invalid project role"


def test_successful_grant_invalidates_the_resolver_cache(
    client_as_owner, role_svc_strict_roster_follows_seed
):
    """The resolver caches role rows for up to its TTL, so without this a grant or revoke would not take
    effect for up to a minute."""
    role_svc_strict_roster_follows_seed.grant.return_value = _row()
    assert (
        client_as_owner.post(
            "/api/v1/projects/proj-1/roles",
            json={"principal_id": "oid-2", "principal_type": "user", "role": "viewer"},
        ).status_code
        == 201
    )
    client_as_owner._project_resolver.invalidate.assert_called_once()


def test_successful_revoke_invalidates_the_resolver_cache(
    client_as_owner, role_svc_strict_roster_follows_seed
):
    role_svc_strict_roster_follows_seed.revoke.return_value = None
    assert client_as_owner.delete("/api/v1/projects/proj-1/roles/oid-1").status_code == 204
    client_as_owner._project_resolver.invalidate.assert_called_once()


def test_failed_write_does_not_invalidate(client_as_owner, role_svc_strict_roster_follows_seed):
    from services.project_role_service import ProjectRoleError

    role_svc_strict_roster_follows_seed.revoke.side_effect = ProjectRoleError("nope", kind="last_owner")
    assert client_as_owner.delete("/api/v1/projects/proj-1/roles/oid-1").status_code == 409
    client_as_owner._project_resolver.invalidate.assert_not_called()


def test_create_project_grants_creator_owner(
    client_as_operator, role_svc_strict_roster_follows_seed, mock_project_svc
):
    r = client_as_operator.post(
        "/api/v1/projects",
        json={"name": "P", "connection_id": "conn-1", "tenant_id": "default"},
    )
    assert r.status_code == 201
    role_svc_strict_roster_follows_seed.grant.assert_called_once()
    assert role_svc_strict_roster_follows_seed.grant.call_args.args[1].role == "owner"


def test_creator_owner_is_keyed_on_oid_not_the_email(client_as_operator, role_svc_strict_roster_follows_seed):
    """``created_by`` is an email and not joinable to an identity; the resolver matches a row's
    ``principal_id`` against the caller's oid or group ids, so the grant is keyed on the oid."""
    r = client_as_operator.post(
        "/api/v1/projects",
        json={"name": "P", "connection_id": "conn-1", "tenant_id": "default"},
    )
    assert r.status_code == 201
    created = role_svc_strict_roster_follows_seed.grant.call_args.args[1]
    assert created.principal_id == "operator-oid"
    assert created.principal_type == "user"
    assert role_svc_strict_roster_follows_seed.grant.call_args.kwargs["granted_by"] == "operator-oid"


def test_creator_owner_grant_failure_does_not_fail_the_create(
    client_as_operator, role_svc_strict_roster_follows_seed
):
    """The project is already persisted when the grant runs, so a store failure is logged, not
    propagated; an ungoverned project is the safe outcome."""
    from services.project_role_service import ProjectRoleError

    role_svc_strict_roster_follows_seed.grant.side_effect = ProjectRoleError("ddb down", kind="validation")
    r = client_as_operator.post(
        "/api/v1/projects",
        json={"name": "P", "connection_id": "conn-1", "tenant_id": "default"},
    )
    assert r.status_code == 201
    assert "ddb down" not in r.text


def test_create_project_skips_the_grant_without_an_oid(
    make_client, role_svc_strict_roster_follows_seed, monkeypatch
):
    """A dev-auth principal has ``oid=None`` and no joinable identity, so the grant is skipped."""
    monkeypatch.setenv("USE_DEV_AUTH", "True")
    monkeypatch.setenv("REGISTRY_IN_MEMORY", "true")
    monkeypatch.setenv("GRAPH_IN_MEMORY", "true")
    client = make_client()
    r = client.post(
        "/api/v1/projects",
        json={"name": "P", "connection_id": "conn-1", "tenant_id": "default"},
        headers={"x-user-role": "operator"},
    )
    assert r.status_code == 201
    role_svc_strict_roster_follows_seed.grant.assert_not_called()


def test_create_project_still_honours_the_tenant_gate(make_client, role_svc_strict_roster_follows_seed):
    """An unknown tenant renders the tenant domain's ``not_found`` row, 404 "Tenant not found", and
    nothing is granted."""
    client = make_client(tenant_ctx=_tenant_context(tenant_ids=["ten-1"]))
    r = client.post(
        "/api/v1/projects",
        json={"name": "P", "connection_id": "conn-1", "tenant_id": "ten-unknown"},
    )
    assert r.status_code == 404
    assert r.json() == {"detail": "Tenant not found", "code": "tenant.not_found"}
    role_svc_strict_roster_follows_seed.grant.assert_not_called()
