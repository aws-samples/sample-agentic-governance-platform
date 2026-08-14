"""Per-project role route tests (Epic 27, Task T3).

Mirrors ``test_projects_routes.py`` exactly: the autouse module reset, the entra env
fixture, FAKE resolvers patched onto the ``users`` module globals (the ONE resolver
singletons), and the services as ``MagicMock``s on the ``projects`` module globals.
NO ``dependency_overrides`` — the REAL ``require_role`` / ``current_principal`` /
``get_tenant_ctx`` / ``get_project_ctx`` chain runs against a mocked
``verify_entra_token`` (no live Entra) and mocked services (no live AWS/GitHub).

What this pins:
  - the two gates COMPOSE and in the right ORDER — ``_load_visible_project`` first
    (foreign tenant 404s before any role logic), then ``may()`` (403 before any write);
  - VIEWER reads the roster, OWNER changes it, MAINTAINER cannot;
  - a global platform admin bypasses the project role entirely;
  - ``granted_by`` comes from the PRINCIPAL, never the body;
  - the store's ``.kind`` → FIXED status + FIXED detail literal mapping;
  - every successful write invalidates the resolver's short-TTL role cache;
  - ``create_project`` bootstraps the creator as OWNER, keyed on ``oid``, and a grant
    failure never fails the create.
"""

from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from models.project import Project, ProjectDetail
from models.project_role import ProjectRole, ProjectRoleRecord

FIXED_TS = "2026-07-27T00:00:00+00:00"
INSUFFICIENT = "insufficient project role"


@pytest.fixture(autouse=True)
def reset_modules():
    import sys

    for mod in [
        "core.rbac",
        "core.security_entra",
        "core.config",
        "api.routes.projects",
        "api.routes.users",
        "api.routes.tenants",
    ]:
        sys.modules.pop(mod, None)
    yield


@pytest.fixture
def entra_settings(monkeypatch):
    monkeypatch.setenv("AUTH_PROVIDER", "entra")
    monkeypatch.setenv("USE_DEV_AUTH", "False")
    monkeypatch.setenv("ENTRA_TENANT_ID", "00000000-0000-0000-0000-000000000001")
    monkeypatch.setenv("ENTRA_AUDIENCE", "api://agp")
    monkeypatch.setenv("ENTRA_BACKEND_CLIENT_ID", "backend-client-id")
    monkeypatch.setenv("ENTRA_SPA_CLIENT_ID", "spa-client-id")
    monkeypatch.setenv("ENTRA_ROLE_ADMIN", "Platform.Admin")
    monkeypatch.setenv("ENTRA_ROLE_OPERATOR", "Platform.Operator")
    monkeypatch.setenv("ENTRA_ROLE_VIEWER", "Platform.Viewer")


# --- record / model helpers --------------------------------------------------

def _row(pid="proj-1", principal="oid-1", role="owner"):
    return ProjectRoleRecord(
        project_id=pid, principal_id=principal, principal_type="user",
        principal_display="Alex", role=role, granted_by="seed", granted_at=FIXED_TS,
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


# --- context + resolver fakes ------------------------------------------------

def _tenant_context(*, is_global=False, tenant_ids=("default",)):
    from services.tenant_resolver import TenantContext

    return TenantContext(is_global=is_global, tenant_ids=frozenset(tenant_ids), tenants=())


def _project_context(*, is_global=False, role=None, project_id="proj-1"):
    from services.project_resolver import ProjectContext

    return ProjectContext(
        is_global=is_global, roles={} if role is None else {project_id: role}
    )


class _FakeTenantResolver:
    """Async ``resolve`` stub returning a fixed TenantContext regardless of principal."""

    def __init__(self, ctx):
        self._ctx = ctx

    async def resolve(self, principal):
        return self._ctx


class _FakeProjectResolver:
    """Async ``resolve`` stub + a spy-able ``invalidate`` (the real resolver's public
    cache-drop, which every role write must call) + the real ``refresh_project`` semantics.

    ``refresh_project`` is NOT stubbed out: the gates call it on every DENY, so a fake without
    it would make the route's defensive except-clause swallow an AttributeError and silently
    stop exercising the fresh-read path. It therefore does exactly what the real resolver does —
    a strict single-project read through the SAME ``_role_svc`` the test seeds, folded by the
    real pure ``context_from_rows``."""

    def __init__(self, ctx):
        self._ctx = ctx
        self.invalidate = MagicMock()

    async def resolve(self, principal):
        return self._ctx

    def refresh_project(self, principal, project_id):
        import api.routes.projects as projects_module
        from services.project_resolver import context_from_rows

        rows = projects_module._role_svc.list_for_project_strict(project_id)
        return context_from_rows(principal, rows)


class _FakeTenantService:
    """Minimal ``TenantService``-shaped fake: only ``.get`` is exercised by create()."""

    def __init__(self, known_ids):
        self._known = set(known_ids)

    def get(self, tenant_id):
        from services.tenant_service import TenantError

        if tenant_id not in self._known:
            raise TenantError("Unknown tenant", kind="not_found")
        return MagicMock(id=tenant_id)


# --- the shared service / client fixtures ------------------------------------

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
def role_svc():
    """The ``ProjectRoleService`` MagicMock on the projects module global.

    The LIST route reads the STRICT roster (E27/T11 — "empty" and "unreadable" must be
    distinguishable on the wire), so ``list_for_project_strict`` is DERIVED from the
    degrading seed rather than seeded separately: a test that sets rows gets them from
    either read, the two can never disagree, and a regression back to the degrading read
    stays visible as a call on ``list_for_project``. Same idiom
    ``test_agent_project_gating`` uses for ``has_role_rows``."""
    import api.routes.projects as projects_module

    svc = MagicMock()
    svc.list_for_project.return_value = []
    svc.list_for_project_strict.side_effect = lambda pid: svc.list_for_project(pid)
    projects_module._role_svc = svc
    return svc


@pytest.fixture
def project_svc():
    """The ``ProjectService`` MagicMock on the projects module global. Defaults to a
    visible project so the tenant gate passes and the role gate is what's under test."""
    import api.routes.projects as projects_module

    svc = MagicMock()
    svc.get_project.return_value = _detail()
    svc.create_project.return_value = _project()
    projects_module._svc = svc
    return svc


def _build_client(*, platform_role, project_ctx, tenant_ctx, role_svc, project_svc):
    import api.routes.projects as projects_module
    import api.routes.tenants as tenants_module
    import api.routes.users as users_module

    # Seed the TWO ONE-per-app resolver singletons that users.py owns. Both the tenant
    # and the project gate reach them through the thin re-exports in projects.py.
    users_module._tenant_resolver = _FakeTenantResolver(tenant_ctx)
    users_module._project_resolver = _FakeProjectResolver(project_ctx)
    tenants_module._svc = _FakeTenantService({"default", "ten-1", "ten-2"})

    app = FastAPI()
    app.include_router(projects_module.router, prefix="/api/v1")
    client = TestClient(app, headers={"Authorization": "Bearer fake-token"})
    patcher = patch(
        "core.security_entra.verify_entra_token", return_value=_claims_for(platform_role)
    )
    patcher.start()
    client._entra_patcher = patcher  # stopped by the fixture's teardown
    client._project_resolver = users_module._project_resolver
    return client


@pytest.fixture
def make_client(entra_settings, role_svc, project_svc):
    """Factory the per-role client fixtures below share; stops the entra patch on teardown."""
    built = []

    def _make(*, platform_role="operator", project_ctx=None, tenant_ctx=None):
        client = _build_client(
            platform_role=platform_role,
            project_ctx=project_ctx if project_ctx is not None else _project_context(),
            tenant_ctx=tenant_ctx if tenant_ctx is not None else _tenant_context(),
            role_svc=role_svc,
            project_svc=project_svc,
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
    """A GLOBAL platform admin — ``may()`` short-circuits True regardless of the roles map."""
    return make_client(platform_role="admin", project_ctx=_project_context(is_global=True))


@pytest.fixture
def client_as_operator(make_client):
    """A plain platform operator holding NO project role (the create-project caller)."""
    return make_client(project_ctx=_project_context())


@pytest.fixture
def client_foreign_tenant(make_client, project_svc):
    """Project-role OWNER but a member of a DIFFERENT tenant — proves the tenant gate
    runs FIRST (404) and the role never gets a say."""
    project_svc.get_project.return_value = _detail(_project(tenant_id="ten-2"))
    return make_client(
        project_ctx=_project_context(role=ProjectRole.OWNER),
        tenant_ctx=_tenant_context(tenant_ids=["ten-1"]),
    )


# ===========================================================================
# The gates
# ===========================================================================

def test_owner_can_list_and_grant(client_as_owner, role_svc):
    role_svc.list_for_project.return_value = [_row()]
    r = client_as_owner.get("/api/v1/projects/proj-1/roles")
    assert r.status_code == 200
    assert r.json()[0]["principal_id"] == "oid-1"

    role_svc.grant.return_value = _row(principal="oid-2", role="maintainer")
    r = client_as_owner.post(
        "/api/v1/projects/proj-1/roles",
        json={"principal_id": "oid-2", "principal_type": "user",
              "principal_display": "Sam", "role": "maintainer",
              # a forged grantor in the body must be IGNORED — ProjectRoleCreate has no
              # such field, and the route sources it from the validated principal.
              "granted_by": "oid-FORGED"},
    )
    assert r.status_code == 201
    # granted_by is the AUTHENTICATED principal's oid, never a body value.
    assert role_svc.grant.call_args.kwargs["granted_by"] == "operator-oid"


def test_viewer_can_list_but_not_grant(client_as_viewer, role_svc):
    role_svc.list_for_project.return_value = [_row()]
    assert client_as_viewer.get("/api/v1/projects/proj-1/roles").status_code == 200

    r = client_as_viewer.post(
        "/api/v1/projects/proj-1/roles",
        json={"principal_id": "oid-9", "principal_type": "user", "role": "viewer"},
    )
    assert r.status_code == 403
    assert r.json()["detail"] == INSUFFICIENT
    role_svc.grant.assert_not_called()          # guard BEFORE write


def test_maintainer_cannot_manage_roles(client_as_maintainer, role_svc):
    r = client_as_maintainer.delete("/api/v1/projects/proj-1/roles/oid-1")
    assert r.status_code == 403
    role_svc.revoke.assert_not_called()


def test_maintainer_can_still_read_the_roster(client_as_maintainer, role_svc):
    """MAINTAINER >= VIEWER — ``may()`` is a threshold, not an equality."""
    role_svc.list_for_project.return_value = [_row()]
    assert client_as_maintainer.get("/api/v1/projects/proj-1/roles").status_code == 200


def test_no_project_role_cannot_even_list(client_as_operator, role_svc):
    """FAIL-CLOSED: a platform operator with zero role rows on the project sees a 403,
    not an empty roster."""
    r = client_as_operator.get("/api/v1/projects/proj-1/roles")
    assert r.status_code == 403
    assert r.json()["detail"] == INSUFFICIENT
    # The ROSTER is never served: the body is the error literal, not a (possibly empty) list.
    assert not isinstance(r.json(), list)
    # The only permitted read on this path is the gate's own stale-cache confirmation (one
    # STRICT single-project read). The refusal must not be reached by SERVING the roster.
    assert role_svc.list_for_project_strict.call_args_list == [call("proj-1")]


def test_platform_viewer_still_403s_before_any_project_logic(make_client, role_svc):
    """The project role is checked IN ADDITION to the platform role, never instead:
    a Platform.Viewer is refused by ``require_role(OPERATOR)`` even holding project OWNER."""
    client = make_client(
        platform_role="viewer", project_ctx=_project_context(role=ProjectRole.OWNER)
    )
    assert client.get("/api/v1/projects/proj-1/roles").status_code == 403
    role_svc.list_for_project.assert_not_called()


def test_foreign_tenant_404s_before_any_role_logic(client_foreign_tenant, role_svc):
    r = client_foreign_tenant.get("/api/v1/projects/proj-1/roles")
    assert r.status_code == 404
    role_svc.list_for_project.assert_not_called()


def test_foreign_tenant_404_is_byte_identical_to_a_missing_project(
    client_foreign_tenant, project_svc
):
    """The 404-not-403 contract: a foreign tenant's project must look ABSENT."""
    foreign = client_foreign_tenant.get("/api/v1/projects/proj-1/roles")
    project_svc.get_project.return_value = None
    missing = client_foreign_tenant.get("/api/v1/projects/truly-missing/roles")
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json() == {"detail": "Project not found"}


def test_owner_of_projA_cannot_manage_projB(make_client, role_svc, project_svc):
    """The gate is scoped PER PROJECT, not "holds the role somewhere".

    The caller is OWNER on proj-1 ONLY. proj-2 is in the SAME tenant, so the tenant gate
    passes and ``may(pctx, id, ...)`` is the only thing between them and proj-2 — which
    means a project-id-agnostic gate (the classic IDOR/copy-paste shape) fails here and
    nowhere else in this file. T4-T8 copy this gate onto twelve more routes.
    """
    project_svc.get_project.return_value = _detail(_project(id="proj-2"))
    client = make_client(
        project_ctx=_project_context(role=ProjectRole.OWNER, project_id="proj-1")
    )
    role_svc.grant.return_value = _row(pid="proj-2")
    r = client.post(
        "/api/v1/projects/proj-2/roles",
        json={"principal_id": "attacker", "principal_type": "user", "role": "owner"},
    )
    assert r.status_code == 403
    assert r.json()["detail"] == INSUFFICIENT
    role_svc.grant.assert_not_called()


def test_owner_of_projA_cannot_even_read_projB_roster(make_client, role_svc, project_svc):
    """The same scoping on the READ verb — VIEWER on proj-1 is not VIEWER on proj-2."""
    project_svc.get_project.return_value = _detail(_project(id="proj-2"))
    client = make_client(
        project_ctx=_project_context(role=ProjectRole.OWNER, project_id="proj-1")
    )
    r = client.get("/api/v1/projects/proj-2/roles")
    assert r.status_code == 403
    # Only proj-2's own gate-confirmation read is allowed; proj-1's roster is never touched,
    # and no roster is served for either project.
    assert role_svc.list_for_project_strict.call_args_list == [call("proj-2")]


def test_admin_bypasses_project_role(client_as_admin, role_svc):
    role_svc.grant.return_value = _row()
    r = client_as_admin.post(
        "/api/v1/projects/proj-1/roles",
        json={"principal_id": "oid-3", "principal_type": "group", "role": "owner"},
    )
    assert r.status_code == 201


# ===========================================================================
# Writes: update / revoke + the error-mapping contract
# ===========================================================================

def test_update_role_uses_the_path_principal_not_the_body(client_as_owner, role_svc):
    """The store's ``grant`` is an upsert keyed on (project, principal), so a PUT IS a
    re-grant — and the PATH id wins so a mismatched body can't retarget the write."""
    role_svc.grant.return_value = _row(principal="oid-1", role="viewer")
    r = client_as_owner.put(
        "/api/v1/projects/proj-1/roles/oid-1",
        json={"principal_id": "oid-EVIL", "principal_type": "user", "role": "viewer",
              "granted_by": "oid-FORGED"},
    )
    assert r.status_code == 200
    assert role_svc.grant.call_args.args[1].principal_id == "oid-1"
    # ...and the grantor is the authenticated principal, not anything the body carried.
    assert role_svc.grant.call_args.kwargs["granted_by"] == "operator-oid"


def test_last_owner_downgrade_via_put_maps_to_409(client_as_owner, role_svc):
    """The PUT verb IS an upsert, so it can strand a project with zero owners exactly like a
    revoke can — the store's guard covers both verbs and both map to the SAME 409 literal."""
    from services.project_role_service import ProjectRoleError
    role_svc.grant.side_effect = ProjectRoleError("secret internals", kind="last_owner")
    r = client_as_owner.put(
        "/api/v1/projects/proj-1/roles/oid-1",
        json={"principal_id": "oid-1", "principal_type": "user", "role": "viewer"},
    )
    assert r.status_code == 409
    assert r.json()["detail"] == "project must keep at least one owner"
    assert "secret internals" not in r.text
    client_as_owner._project_resolver.invalidate.assert_not_called()


def test_last_owner_downgrade_via_post_maps_to_409(client_as_owner, role_svc):
    """POST hits the SAME upsert as PUT, so a body naming the sole owner with a LESSER role
    is the same zero-owner lockout — 409 with the pinned literal, not the bare-raise 500.
    Without this the branch looks like duplicated PUT handling and a tidy-up refactor would
    silently restore the 500."""
    from services.project_role_service import ProjectRoleError
    role_svc.grant.side_effect = ProjectRoleError("secret internals", kind="last_owner")
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
    client_as_owner, role_svc, verb
):
    """The store refuses a role change it cannot prove is safe (unreadable role partition)
    — a transient store fault, so 503 with a FIXED literal, never ``str(err)``."""
    from services.project_role_service import ProjectRoleError
    role_svc.grant.side_effect = ProjectRoleError(
        "ProvisionedThroughputExceededException", kind="ownership_unverified"
    )
    body = {"principal_id": "oid-1", "principal_type": "user", "role": "viewer"}
    url = (
        "/api/v1/projects/proj-1/roles"
        if verb == "post"
        else "/api/v1/projects/proj-1/roles/oid-1"
    )
    r = getattr(client_as_owner, verb)(url, json=body)
    assert r.status_code == 503
    assert r.json()["detail"] == "could not verify project ownership"
    assert "ProvisionedThroughputExceededException" not in r.text
    client_as_owner._project_resolver.invalidate.assert_not_called()


# ===========================================================================
# The ROSTER read must not answer a store fault with 200 + [] (E27/T11)
#
# The console USES this list's emptiness as a decision: the Access tab's ``existingIds``
# is what makes its Grant an ADD rather than reaching the backend's UPSERT, so a
# falsely-empty roster lets "grant Viewer" silently DOWNGRADE a principal who already
# holds Owner. "Empty" and "unreadable" are the same value to the degrading read but
# opposite answers to the browser — so the route reads the STRICT roster and surfaces the
# SAME 503 + literal T3 already pins for an unverifiable-ownership read.
# ===========================================================================

def test_unreadable_roster_is_503_not_an_empty_list(client_as_owner, role_svc):
    """The reproducer for the silent-owner-downgrade path: the roster read must FAIL, not
    come back empty, when the role partition is unreadable."""
    from services.project_role_service import ProjectRoleError
    role_svc.list_for_project_strict.side_effect = ProjectRoleError(
        "ProvisionedThroughputExceededException", kind="ownership_unverified"
    )
    r = client_as_owner.get("/api/v1/projects/proj-1/roles")
    assert r.status_code == 503, "a store fault must never read as an empty roster"
    assert r.json()["detail"] == "could not verify project ownership"
    assert "ProvisionedThroughputExceededException" not in r.text


def test_the_roster_route_reads_the_strict_loader(client_as_owner, role_svc):
    """Pins WHICH read serves the roster. The degrading ``list_for_project`` swallows a
    ``ClientError`` and returns ``[]``, which is exactly the false 200 above."""
    role_svc.list_for_project.return_value = [_row()]
    assert client_as_owner.get("/api/v1/projects/proj-1/roles").status_code == 200
    role_svc.list_for_project_strict.assert_called_once_with("proj-1")


def test_a_genuinely_empty_roster_is_still_200(client_as_owner, role_svc):
    """Readable-and-empty is a real answer — the strict read must not turn it into a 503."""
    role_svc.list_for_project.return_value = []
    r = client_as_owner.get("/api/v1/projects/proj-1/roles")
    assert r.status_code == 200
    assert r.json() == []


def test_revoke_returns_204(client_as_owner, role_svc):
    role_svc.revoke.return_value = None
    r = client_as_owner.delete("/api/v1/projects/proj-1/roles/oid-1")
    assert r.status_code == 204
    role_svc.revoke.assert_called_once_with("proj-1", "oid-1")


def test_last_owner_revoke_maps_to_409(client_as_owner, role_svc):
    from services.project_role_service import ProjectRoleError
    role_svc.revoke.side_effect = ProjectRoleError("nope", kind="last_owner")
    r = client_as_owner.delete("/api/v1/projects/proj-1/roles/oid-1")
    assert r.status_code == 409
    assert r.json()["detail"] == "project must keep at least one owner"


def test_revoke_unknown_grant_maps_to_404(client_as_owner, role_svc):
    from services.project_role_service import ProjectRoleError
    role_svc.revoke.side_effect = ProjectRoleError("secret internals", kind="not_found")
    r = client_as_owner.delete("/api/v1/projects/proj-1/roles/oid-nope")
    assert r.status_code == 404
    assert r.json()["detail"] == "project role not found"
    assert "secret internals" not in r.text


def test_grant_validation_error_maps_to_400_with_a_fixed_detail(client_as_owner, role_svc):
    """T1's store rejects a bad role name AND a '#' in either key component with
    kind="validation" — both surface as the SAME fixed 400, never ``str(err)``."""
    from services.project_role_service import ProjectRoleError
    role_svc.grant.side_effect = ProjectRoleError("secret internals", kind="validation")
    r = client_as_owner.post(
        "/api/v1/projects/proj-1/roles",
        json={"principal_id": "oid#evil", "principal_type": "user", "role": "owner"},
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "invalid project role"
    assert "secret internals" not in r.text


# ===========================================================================
# The resolver cache must not outlive a role change
# ===========================================================================

def test_successful_grant_invalidates_the_resolver_cache(client_as_owner, role_svc):
    """The resolver caches role rows for up to its TTL, so without this a grant/revoke
    would not take effect for up to a minute."""
    role_svc.grant.return_value = _row()
    assert client_as_owner.post(
        "/api/v1/projects/proj-1/roles",
        json={"principal_id": "oid-2", "principal_type": "user", "role": "viewer"},
    ).status_code == 201
    client_as_owner._project_resolver.invalidate.assert_called_once()


def test_successful_revoke_invalidates_the_resolver_cache(client_as_owner, role_svc):
    role_svc.revoke.return_value = None
    assert client_as_owner.delete(
        "/api/v1/projects/proj-1/roles/oid-1"
    ).status_code == 204
    client_as_owner._project_resolver.invalidate.assert_called_once()


def test_failed_write_does_not_invalidate(client_as_owner, role_svc):
    from services.project_role_service import ProjectRoleError
    role_svc.revoke.side_effect = ProjectRoleError("nope", kind="last_owner")
    assert client_as_owner.delete(
        "/api/v1/projects/proj-1/roles/oid-1"
    ).status_code == 409
    client_as_owner._project_resolver.invalidate.assert_not_called()


# ===========================================================================
# Creator-OWNER bootstrap
# ===========================================================================

def test_create_project_grants_creator_owner(client_as_operator, role_svc, project_svc):
    r = client_as_operator.post(
        "/api/v1/projects",
        json={"name": "P", "connection_id": "conn-1", "tenant_id": "default"},
    )
    assert r.status_code == 201
    role_svc.grant.assert_called_once()
    assert role_svc.grant.call_args.args[1].role == "owner"


def test_creator_owner_is_keyed_on_oid_not_the_email(client_as_operator, role_svc):
    """``created_by`` is an email and is NOT joinable to an identity — the resolver matches
    a row's ``principal_id`` against the caller's oid ∪ group ids, so the grant must be
    keyed on the oid."""
    r = client_as_operator.post(
        "/api/v1/projects",
        json={"name": "P", "connection_id": "conn-1", "tenant_id": "default"},
    )
    assert r.status_code == 201
    created = role_svc.grant.call_args.args[1]
    assert created.principal_id == "operator-oid"
    assert created.principal_type == "user"
    # granted_by is the principal's own identity, never a body value.
    assert role_svc.grant.call_args.kwargs["granted_by"] == "operator-oid"


def test_creator_owner_grant_failure_does_not_fail_the_create(client_as_operator, role_svc):
    """The project is already persisted when the grant runs — a store failure must be LOGGED,
    not propagated, or a 500 would orphan a project that exists. An ungoverned project is the
    safe outcome (the gate is fail-closed for non-admins)."""
    from services.project_role_service import ProjectRoleError
    role_svc.grant.side_effect = ProjectRoleError("ddb down", kind="validation")
    r = client_as_operator.post(
        "/api/v1/projects",
        json={"name": "P", "connection_id": "conn-1", "tenant_id": "default"},
    )
    assert r.status_code == 201
    assert "ddb down" not in r.text


def test_create_project_skips_the_grant_without_an_oid(make_client, role_svc, monkeypatch):
    """A dev-auth principal has ``oid=None`` — no joinable identity, so the grant is skipped
    and logged rather than keyed on something the resolver could never match."""
    monkeypatch.setenv("USE_DEV_AUTH", "True")
    client = make_client()
    r = client.post(
        "/api/v1/projects",
        json={"name": "P", "connection_id": "conn-1", "tenant_id": "default"},
        headers={"x-user-role": "operator"},
    )
    assert r.status_code == 201
    role_svc.grant.assert_not_called()


def test_create_project_still_honours_the_tenant_gate(make_client, role_svc):
    """The bootstrap is bolted on AFTER the existing create semantics — an unknown tenant
    still 400s and NOTHING is granted."""
    client = make_client(tenant_ctx=_tenant_context(tenant_ids=["ten-1"]))
    r = client.post(
        "/api/v1/projects",
        json={"name": "P", "connection_id": "conn-1", "tenant_id": "ten-unknown"},
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "unknown tenant"
    role_svc.grant.assert_not_called()
