"""Per-project role GATING of the pre-existing project routes (Epic 27, Task T4).

T3 added the role store + the ``get_project_ctx`` dependency and gated the role-CRUD
routes. THIS file pins the same gate on the TEN pre-existing project/repository routes,
which is where the gate actually earns its keep: after this, the E23 delete cascade
(a live AgentCore runtime, an Entra app registration, ECR images and the registry
record) needs a real project OWNER row.

Fixture idiom is ``test_project_roles_routes.py`` verbatim: the autouse module reset,
the entra env fixture, FAKE resolvers on the ``users`` module globals (the ONE resolver
singletons), the services as ``MagicMock``s on the ``projects`` module globals, and NO
``dependency_overrides`` — the REAL ``require_role`` / ``current_principal`` /
``get_tenant_ctx`` / ``get_project_ctx`` chain runs against a mocked
``verify_entra_token``.

What this pins:
  - the required role PER ROUTE (viewer read / maintainer materialize / owner destroy),
    and that a caller one level BELOW it gets 403 with the fixed literal;
  - repo delete needs OWNER, not MAINTAINER, and is refused BEFORE the cascade runs;
  - the ungoverned-project fallback (design §3): a project with ZERO role rows keeps
    today's semantics for maintainer-level verbs so the migration is not a flag day —
    but OWNER verbs still demand a real row;
  - ORDER: ``_load_visible_project`` first, so a foreign tenant still 404s (never a 403
    that would confirm the project exists) before any role logic;
  - the two LIST routes filter to the projects the caller may see, with ONE role-store
    read (not one per project);
  - a global platform admin bypasses the project role entirely.
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

def _governed_row(pid="proj-1", principal="someone-else-oid", role="owner"):
    """A role row on ``pid`` — its mere EXISTENCE is what makes the project 'governed',
    so the zero-rows fallback no longer applies to it."""
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


# --- context + resolver fakes ------------------------------------------------

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
    """Async ``resolve`` stub + the real ``refresh_project`` semantics.

    ``refresh_project`` is NOT stubbed out: the gates call it on every DENY, so a fake without
    it would make the route's defensive except-clause swallow an AttributeError and silently
    stop exercising the fresh-read path. It therefore does exactly what the real resolver does —
    a strict single-project read through the SAME ``_role_svc`` the test seeds, folded by the
    real pure ``context_from_rows`` — so a stale-cache test can seed rows the cached ctx omits."""

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


# --- the shared service / client fixtures ------------------------------------

@pytest.fixture
def role_svc():
    """The ``ProjectRoleService`` MagicMock on the projects module global. Defaults to an
    UNGOVERNED project (zero rows) — each test opts into governance explicitly.

    The GATE reads the STRICT pair (``has_role_rows`` / ``list_all_strict``), which is what
    lets an unreadable partition raise instead of degrading to "ungoverned"; the degrading
    pair is seeded too so an accidental switch back to it shows up as a governance change
    rather than a silent pass on a truthy auto-attribute."""
    import api.routes.projects as projects_module

    svc = MagicMock()
    svc.has_role_rows.return_value = False
    svc.list_all_strict.return_value = []
    svc.list_for_project.return_value = []
    svc.list_all.return_value = []
    projects_module._role_svc = svc
    return svc


def _governed(role_svc, *pids):
    """Mark the given project ids GOVERNED for BOTH gate inputs — the single-project gate
    (``has_role_rows``) and the LIST gate (``list_all_strict``)."""
    ids = set(pids) or {"proj-1"}
    role_svc.has_role_rows.side_effect = lambda pid: pid in ids
    role_svc.list_all_strict.return_value = [_governed_row(pid=p) for p in sorted(ids)]


@pytest.fixture
def project_svc():
    """The ``ProjectService`` MagicMock on the projects module global. Defaults to a
    VISIBLE project + a repo under it so the tenant gate passes and the ROLE gate is
    what's under test."""
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
def client_factory(entra_settings, role_svc, project_svc):
    """Build a TestClient for a caller with a given PLATFORM role + PROJECT role."""
    built = []

    def _make(*, platform_role="operator", project_role=None, roles=None,
              project_ctx=None, tenant_ctx=None):
        import api.routes.projects as projects_module
        import api.routes.tenants as tenants_module
        import api.routes.users as users_module

        users_module._tenant_resolver = _FakeTenantResolver(
            tenant_ctx if tenant_ctx is not None else _tenant_context()
        )
        users_module._project_resolver = _FakeProjectResolver(
            project_ctx
            if project_ctx is not None
            else _project_context(
                is_global=platform_role == "admin", role=project_role, roles=roles
            )
        )
        tenants_module._svc = _FakeTenantService({"default", "ten-1", "ten-2"})

        app = FastAPI()
        app.include_router(projects_module.router, prefix="/api/v1")
        app.include_router(projects_module.repositories_router, prefix="/api/v1")
        client = TestClient(app, headers={"Authorization": "Bearer fake-token"})
        patcher = patch(
            "core.security_entra.verify_entra_token", return_value=_claims_for(platform_role)
        )
        patcher.start()
        client._entra_patcher = patcher
        built.append(client)
        return client

    yield _make
    for client in built:
        client._entra_patcher.stop()


# ===========================================================================
# The per-route threshold
# ===========================================================================

# (method, path, json body, required role) — the T4 contract table.
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
    """One request, table-driven. ``httpx``'s ``get``/``delete`` take no ``json=``, so a
    bodyless verb goes through the plain method and a bodied one through ``request``."""
    if body is None:
        return getattr(client, method)(path)
    return client.request(method.upper(), path, json=body)


@pytest.mark.parametrize("method,path,body,required_role", _GATED)
def test_route_requires_its_role(method, path, body, required_role, client_factory, role_svc):
    """A caller one level BELOW the required role is refused with the fixed literal."""
    below = _ONE_BELOW[required_role]
    if below is None:
        pytest.skip("viewer is the floor")
    _governed(role_svc, "proj-1")
    client = client_factory(project_role=below)
    r = _call(client, method, path, body)
    assert r.status_code == 403
    assert r.json()["detail"] == INSUFFICIENT


@pytest.mark.parametrize("method,path,body,required_role", _GATED)
def test_route_admits_exactly_its_role(
    method, path, body, required_role, client_factory, role_svc
):
    """...and the caller who DOES hold it gets through (the gate is a threshold, not a wall)."""
    _governed(role_svc, "proj-1")
    client = client_factory(project_role=required_role)
    r = _call(client, method, path, body)
    assert r.status_code < 400, r.text


@pytest.mark.parametrize("method,path,body,required_role", _GATED)
def test_no_project_role_on_a_governed_project_is_refused(
    method, path, body, required_role, client_factory, role_svc
):
    """FAIL-CLOSED on a GOVERNED project: rows exist but none of them are the caller's,
    so even the VIEWER floor is a 403 — not an empty/partial success."""
    _governed(role_svc, "proj-1")
    client = client_factory(project_role=None)
    r = _call(client, method, path, body)
    assert r.status_code == 403
    assert r.json()["detail"] == INSUFFICIENT


def test_repo_delete_requires_owner_not_maintainer(client_factory, role_svc, project_svc):
    """The E23 cascade (runtime + Entra app + ECR images + record) is an OWNER act."""
    _governed(role_svc, "proj-1")
    client = client_factory(project_role="maintainer")
    r = client.delete("/api/v1/projects/proj-1/repos/r-1")
    assert r.status_code == 403
    project_svc.delete_repo.assert_not_called()  # guard BEFORE the cascade


def test_project_delete_requires_owner_not_maintainer(client_factory, role_svc, project_svc):
    _governed(role_svc, "proj-1")
    client = client_factory(project_role="maintainer")
    assert client.delete("/api/v1/projects/proj-1").status_code == 403
    project_svc.delete_project.assert_not_called()


def test_gate_is_scoped_per_project_not_globally_held(client_factory, role_svc, project_svc):
    """OWNER on proj-1 is NOT owner on proj-2 — the classic IDOR/copy-paste shape where a
    gate forgets to pass the path id."""
    project_svc.get_project.return_value = _detail(_project(id="proj-2"))
    _governed(role_svc, "proj-2")
    client = client_factory(roles={"proj-1": "owner"})
    r = client.delete("/api/v1/projects/proj-2")
    assert r.status_code == 403
    project_svc.delete_project.assert_not_called()


# ===========================================================================
# Ordering: the tenant gate runs FIRST and never leaks existence
# ===========================================================================

@pytest.mark.parametrize("method,path,body,required_role", _GATED)
def test_foreign_tenant_404s_before_any_role_logic(
    method, path, body, required_role, client_factory, role_svc, project_svc
):
    """A foreign tenant's project must look ABSENT (404), never 403 — a 403 would confirm
    it exists. So the role store is never even consulted.

    The caller holds NO role and the project IS governed, so the ROLE gate would refuse this
    request with a 403 on its own. That is what makes the ORDER observable: only
    ``_load_visible_project`` running FIRST can produce the 404. A caller who holds the role
    (e.g. OWNER) would let ``may()`` short-circuit True and the gate would pass silently in
    EITHER order, so this test would not be able to fail — see
    ``test_gate_order_is_observable_on_every_gated_route`` for the same property stated as an
    invariant across the whole table."""
    project_svc.get_project.return_value = _detail(_project(tenant_id="ten-2"))
    _governed(role_svc, "proj-1")
    client = client_factory(
        project_role=None, tenant_ctx=_tenant_context(tenant_ids=["ten-1"])
    )
    r = _call(client, method, path, body)
    assert r.status_code == 404
    assert r.json()["detail"] == "Project not found"
    role_svc.has_role_rows.assert_not_called()


@pytest.mark.parametrize("method,path,body,required_role", _GATED)
def test_gate_order_is_observable_on_every_gated_route(
    method, path, body, required_role, client_factory, role_svc, project_svc
):
    """The ordering contract, pinned per route so it CANNOT pass for the wrong reason.

    Setup: the caller would be refused by the role gate (no role, governed project) AND the
    project is in a foreign tenant. The two gates therefore disagree — tenant-first says 404,
    role-first says 403 — so the observed response distinguishes the order rather than merely
    observing a status code. Byte-identical to the truly-missing 404 is the actual property:
    a 403 here would CONFIRM that a foreign tenant's project exists."""
    project_svc.get_project.return_value = _detail(_project(tenant_id="ten-2"))
    _governed(role_svc, "proj-1")
    client = client_factory(
        project_role=None, tenant_ctx=_tenant_context(tenant_ids=["ten-1"])
    )
    foreign = _call(client, method, path, body)

    # The same request against an id that genuinely does not exist, for the byte comparison.
    project_svc.get_project.return_value = None
    missing = _call(client, method, path.replace("proj-1", "truly-missing"), body)

    assert foreign.status_code == missing.status_code == 404, foreign.text
    assert foreign.json() == missing.json() == {"detail": "Project not found"}


def test_foreign_tenant_404_is_byte_identical_to_a_missing_project(
    client_factory, role_svc, project_svc
):
    project_svc.get_project.return_value = _detail(_project(tenant_id="ten-2"))
    _governed(role_svc, "proj-1")
    client = client_factory(
        project_role=None, tenant_ctx=_tenant_context(tenant_ids=["ten-1"])
    )
    foreign = client.delete("/api/v1/projects/proj-1")
    project_svc.get_project.return_value = None
    missing = client.delete("/api/v1/projects/truly-missing")
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json() == {"detail": "Project not found"}


# ===========================================================================
# The ungoverned-project fallback (design §3) — migration is not a flag day
# ===========================================================================

def test_ungoverned_project_allows_maintainer_verbs(client_factory, role_svc, project_svc):
    """Zero role rows == today's semantics, so migration is not a flag day."""
    role_svc.has_role_rows.return_value = False
    client = client_factory(project_role=None)
    r = client.post("/api/v1/projects/proj-1/repos", json=_valid_repo_body())
    assert r.status_code == 202
    project_svc.add_repo.assert_called_once()


def test_ungoverned_project_allows_viewer_verbs(client_factory, role_svc):
    role_svc.has_role_rows.return_value = False
    client = client_factory(project_role=None)
    assert client.get("/api/v1/projects/proj-1").status_code == 200
    assert client.get(
        "/api/v1/projects/proj-1/repos/r-1/status"
    ).status_code == 200


def test_ungoverned_project_still_blocks_owner_verbs(client_factory, role_svc, project_svc):
    role_svc.has_role_rows.return_value = False
    client = client_factory(project_role=None)
    r = client.delete("/api/v1/projects/proj-1")
    assert r.status_code == 403
    assert r.json()["detail"] == INSUFFICIENT
    project_svc.delete_project.assert_not_called()


def test_ungoverned_project_still_blocks_the_repo_delete_cascade(
    client_factory, role_svc, project_svc
):
    """The most destructive verb in the epic must NEVER ride the fallback."""
    role_svc.has_role_rows.return_value = False
    client = client_factory(project_role="maintainer")
    assert client.delete("/api/v1/projects/proj-1/repos/r-1").status_code == 403
    assert client.get(
        "/api/v1/projects/proj-1/repos/r-1/delete-preview"
    ).status_code == 403
    project_svc.delete_repo.assert_not_called()
    project_svc.preview_delete.assert_not_called()


def test_governed_path_costs_no_extra_store_read(client_factory, role_svc):
    """``has_rows`` is consulted ONLY when ``may()`` already said no — a caller who holds
    the role must not pay for a role-partition read on every request."""
    client = client_factory(project_role="owner")
    assert client.delete("/api/v1/projects/proj-1").status_code == 204
    role_svc.has_role_rows.assert_not_called()


def test_malformed_project_id_fails_closed(client_factory, role_svc, project_svc):
    """The store rejects a ``#`` in an id (kind="validation"). An id it cannot even read
    rows for must NOT be treated as 'ungoverned' and handed the fallback."""
    from services.project_role_service import ProjectRoleError

    project_svc.get_project.return_value = _detail(_project(id="proj#1"))
    role_svc.has_role_rows.side_effect = ProjectRoleError("bad id", kind="validation")
    client = client_factory(project_role=None)
    r = client.post("/api/v1/projects/proj%231/repos", json=_valid_repo_body())
    assert r.status_code == 403
    project_svc.add_repo.assert_not_called()


# ===========================================================================
# A store READ FAILURE must never hand out the §3 fallback
#
# The whole fallback turns on ONE bit — "does this project hold any role rows?" — and a
# DEGRADING read answers "no" for BOTH an ungoverned project and an unreadable partition.
# Those are opposite authorization answers, so the gate reads the STRICT pair and fails
# closed. Without this, one transient DDB fault would make every governed project in the
# tenant look ungoverned for the duration.
# ===========================================================================

def _unverifiable(role_svc):
    """Both strict gate reads fail the way an unreadable DDB partition surfaces."""
    from services.project_role_service import ProjectRoleError

    boom = ProjectRoleError("Could not verify project ownership", kind="ownership_unverified")
    role_svc.has_role_rows.side_effect = boom
    role_svc.list_all_strict.side_effect = boom


def test_store_read_failure_does_not_grant_the_fallback_on_a_write(
    client_factory, role_svc, project_svc
):
    """The reproducer: a role-less caller must NOT get 202 into someone else's project just
    because the role partition was briefly unreadable."""
    _unverifiable(role_svc)
    client = client_factory(project_role=None)
    r = client.post("/api/v1/projects/proj-1/repos", json=_valid_repo_body())
    assert r.status_code == 403
    assert r.json()["detail"] == INSUFFICIENT
    project_svc.add_repo.assert_not_called()  # no repo, no agent, no Entra app registration


def test_store_read_failure_does_not_leak_governed_projects_in_the_list(
    client_factory, role_svc, project_svc
):
    """``GET /projects`` must not fall back to an EMPTY governed set (= "nothing is
    governed"), which is the maximally permissive answer and leaks the whole inventory."""
    project_svc.list_projects.return_value = [_project("proj-1"), _project("proj-2")]
    _unverifiable(role_svc)
    client = client_factory(project_role=None)
    r = client.get("/api/v1/projects")
    assert r.status_code == 503
    assert r.json()["detail"] == "could not verify project ownership"


def test_store_read_failure_does_not_leak_repositories_in_the_list(
    client_factory, role_svc, project_svc
):
    """Same for ``GET /repositories`` — a repo inherits its parent project's authority, so
    an unverifiable governed set must not widen the page either."""
    project_svc.list_repositories.return_value = [
        _repository(id="r-mine", project_id="proj-1"),
        _repository(id="r-theirs", project_id="proj-2"),
    ]
    project_svc.list_projects.return_value = [_project("proj-1"), _project("proj-2")]
    _unverifiable(role_svc)
    client = client_factory(project_role=None)
    r = client.get("/api/v1/repositories")
    assert r.status_code == 503
    assert r.json()["detail"] == "could not verify project ownership"


def test_store_read_failure_never_leaks_the_store_message(client_factory, project_svc):
    """End-to-end through the REAL service (no mock in the middle): a live DDB
    ``ClientError`` becomes the FIXED 503 literal and its message — table arn, error code —
    never reaches the HTTP body. No ``str(exc)`` in a detail, ever."""
    from botocore.exceptions import ClientError

    import api.routes.projects as projects_module
    from services.project_role_service import ProjectRoleService

    real = ProjectRoleService(table_name="")
    real.table_name = "projects"  # flip to DDB mode with a table that only ever raises
    real._table = MagicMock()
    real._table.query.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "arn:aws:dynamodb:secret"}},
        "Query",
    )
    client = client_factory(project_role=None)
    projects_module._role_svc = real

    r = client.get("/api/v1/projects")
    assert r.status_code == 503
    assert r.json() == {"detail": "could not verify project ownership"}
    assert "dynamodb" not in r.text.lower() and "AccessDenied" not in r.text


def test_a_genuinely_ungoverned_project_still_works(client_factory, role_svc, project_svc):
    """The other half of the contract: failing closed on an UNREADABLE partition must not
    also break the READABLE-and-empty case — design §3's whole point is that pre-migration
    projects keep functioning."""
    role_svc.has_role_rows.return_value = False       # read SUCCEEDED, project has no rows
    role_svc.list_all_strict.return_value = []
    project_svc.list_projects.return_value = [_project("proj-1")]
    client = client_factory(project_role=None)
    assert client.post(
        "/api/v1/projects/proj-1/repos", json=_valid_repo_body()
    ).status_code == 202
    assert client.get("/api/v1/projects/proj-1").status_code == 200
    assert [p["id"] for p in client.get("/api/v1/projects").json()] == ["proj-1"]


# ===========================================================================
# The two LIST routes filter instead of refusing
# ===========================================================================

def test_list_projects_filters_to_visible_roles(client_factory, role_svc, project_svc):
    """Both projects are GOVERNED; the caller holds a role on only one of them."""
    project_svc.list_projects.return_value = [_project("proj-1"), _project("proj-2")]
    role_svc.list_all_strict.return_value = [
        _governed_row(pid="proj-1"), _governed_row(pid="proj-2")
    ]
    client = client_factory(roles={"proj-1": "viewer"})
    body = client.get("/api/v1/projects").json()
    assert [p["id"] for p in body] == ["proj-1"]


def test_list_projects_keeps_ungoverned_projects(client_factory, role_svc, project_svc):
    """The §3 fallback applies to the LIST too — otherwise every pre-migration project
    would vanish from the UI for non-admins the moment this ships."""
    project_svc.list_projects.return_value = [_project("proj-1"), _project("proj-2")]
    role_svc.list_all_strict.return_value = [_governed_row(pid="proj-2")]
    client = client_factory(project_role=None)
    body = client.get("/api/v1/projects").json()
    assert [p["id"] for p in body] == ["proj-1"]  # proj-2 is governed by someone else


def test_list_projects_reads_the_role_store_once(client_factory, role_svc, project_svc):
    """ONE ``list_all_strict()`` for the whole page — never an N+1 ``has_role_rows`` per row."""
    project_svc.list_projects.return_value = [_project(f"proj-{i}") for i in range(5)]
    role_svc.list_all_strict.return_value = []
    client = client_factory(project_role=None)
    assert client.get("/api/v1/projects").status_code == 200
    assert role_svc.list_all_strict.call_count == 1
    role_svc.has_role_rows.assert_not_called()


def test_list_repositories_filters_by_parent_project_role(
    client_factory, role_svc, project_svc
):
    """Repositories carry NO role of their own — authority is inherited from the PARENT
    project, exactly like tenant visibility is."""
    project_svc.list_repositories.return_value = [
        _repository(id="r-mine", project_id="proj-1"),
        _repository(id="r-theirs", project_id="proj-2"),
    ]
    project_svc.list_projects.return_value = [_project("proj-1"), _project("proj-2")]
    role_svc.list_all_strict.return_value = [
        _governed_row(pid="proj-1"), _governed_row(pid="proj-2")
    ]
    client = client_factory(roles={"proj-1": "viewer"})
    body = client.get("/api/v1/repositories").json()
    assert [r["id"] for r in body] == ["r-mine"]


def test_list_repositories_reads_the_role_store_once(client_factory, role_svc, project_svc):
    project_svc.list_repositories.return_value = [
        _repository(id=f"r-{i}", project_id=f"proj-{i}") for i in range(5)
    ]
    project_svc.list_projects.return_value = [_project(f"proj-{i}") for i in range(5)]
    role_svc.list_all_strict.return_value = []
    client = client_factory(project_role=None)
    assert client.get("/api/v1/repositories").status_code == 200
    assert role_svc.list_all_strict.call_count == 1
    role_svc.has_role_rows.assert_not_called()


# ===========================================================================
# POST /projects keeps NO project gate; the global admin bypasses everything
# ===========================================================================

def test_create_project_has_no_project_gate(client_factory, role_svc, project_svc):
    """There is no project to hold a role ON yet — tenant membership is the only gate, and
    the creator becomes OWNER via T3's bootstrap."""
    client = client_factory(project_role=None)
    r = client.post(
        "/api/v1/projects",
        json={"name": "P", "connection_id": "conn-1", "tenant_id": "default"},
    )
    assert r.status_code == 201
    project_svc.create_project.assert_called_once()


@pytest.mark.parametrize("method,path,body,required_role", _GATED)
def test_admin_sees_and_may_everything(
    method, path, body, required_role, client_factory, role_svc
):
    _governed(role_svc, "proj-1")
    client = client_factory(platform_role="admin")
    r = _call(client, method, path, body)
    assert r.status_code < 400, r.text
    # A global admin short-circuits ``may()``, so the role partition is never read.
    role_svc.has_role_rows.assert_not_called()


def test_admin_sees_every_project_in_both_lists(client_factory, role_svc, project_svc):
    project_svc.list_projects.return_value = [_project("proj-1"), _project("proj-2")]
    project_svc.list_repositories.return_value = [
        _repository(id="r-1", project_id="proj-1"),
        _repository(id="r-2", project_id="proj-2"),
    ]
    role_svc.list_all_strict.return_value = [
        _governed_row(pid="proj-1"), _governed_row(pid="proj-2")
    ]
    client = client_factory(
        platform_role="admin", tenant_ctx=_tenant_context(is_global=True)
    )
    assert {p["id"] for p in client.get("/api/v1/projects").json()} == {"proj-1", "proj-2"}
    assert {r["id"] for r in client.get("/api/v1/repositories").json()} == {"r-1", "r-2"}
    # Same short-circuit as the single-project gate, on the LIST side: ``may()`` is already
    # True for a global context, so neither list may touch the strict whole-partition read.
    # Without this, an admin would inherit the 503 from an unreadable role partition instead
    # of their data — and the mutant that drops the short-circuit stays green, because the
    # ids above still match (global ``may()`` admits every row anyway).
    role_svc.list_all_strict.assert_not_called()


# ===========================================================================
# E27/T11 — the detail read reports the caller's own standing as a UI HINT
#
# The browser cannot compute this: a role may be granted to an Entra GROUP, and no
# client-side signal evaluates group membership. Without the hint the frontend either hides
# Grant/Promote from a group-derived OWNER (the design-§9 recommended shape) or renders them
# optimistically and lets a 403 be the answer. It is a HINT ONLY — the gates above are
# still the enforcement, which is why these tests assert the reported value AGREES with what
# the gate actually allows rather than treating it as a permission.
# ===========================================================================

def test_detail_reports_the_callers_effective_role(client_factory, role_svc):
    _governed(role_svc, "proj-1")
    client = client_factory(project_role="maintainer")
    body = client.get("/api/v1/projects/proj-1").json()
    assert body["effective_role"] == "maintainer"
    assert body["ungoverned"] is False


def test_detail_reports_a_group_derived_owner_as_owner(client_factory, role_svc):
    """THE reason this field exists. The caller has NO direct row — their OWNER comes from a
    group the resolver matched — so the roster alone would show them as role-less."""
    _governed(role_svc, "proj-1")
    client = client_factory(roles={"proj-1": "owner"})
    body = client.get("/api/v1/projects/proj-1").json()
    assert body["effective_role"] == "owner"


def test_detail_reports_a_global_admin_as_owner(client_factory, role_svc):
    """An admin's ``may()`` short-circuits True for every verb, so anything weaker than
    "owner" would hide their own buttons from them."""
    _governed(role_svc, "proj-1")
    client = client_factory(platform_role="admin")
    body = client.get("/api/v1/projects/proj-1").json()
    assert body["effective_role"] == "owner"


def test_detail_reports_no_role_as_null_on_an_ungoverned_project(client_factory, role_svc):
    """The §3 fallback case: the caller holds NOTHING (so ``effective_role`` is null — the
    honest answer for the roster and for the STRICT gates), but the project is ungoverned, so
    maintainer-level verbs WILL succeed. That is what the second field says — folding it into
    ``effective_role`` as "maintainer" would claim a standing they do not hold."""
    role_svc.has_role_rows.return_value = False
    client = client_factory(project_role=None)
    body = client.get("/api/v1/projects/proj-1").json()
    assert body["effective_role"] is None
    assert body["ungoverned"] is True


def test_the_hint_agrees_with_what_the_gates_actually_allow(client_factory, role_svc, project_svc):
    """The UI must never show an affordance the backend then refuses. An ungoverned project
    reports ``effective_role: null`` + ``ungoverned: true`` — and the OWNER verb is indeed
    refused while the MAINTAINER verb indeed succeeds."""
    role_svc.has_role_rows.return_value = False
    client = client_factory(project_role=None)
    body = client.get("/api/v1/projects/proj-1").json()
    assert body["effective_role"] is None and body["ungoverned"] is True
    # maintainer-level verb: allowed by the fallback, exactly as ``ungoverned`` advertises.
    assert client.post("/api/v1/projects/proj-1/repos", json=_valid_repo_body()).status_code == 202
    # owner-level verb: refused, which is why the fallback is NOT reported as a held role.
    assert client.delete("/api/v1/projects/proj-1").status_code == 403


def test_the_hint_never_widens_the_gate(client_factory, role_svc, project_svc):
    """A hint is not an authority. Even though the detail read would report ``owner`` for a
    global admin, a NON-global caller on a GOVERNED project gets 403 on the OWNER verb — the
    field is serialized output, never an input to a decision."""
    _governed(role_svc, "proj-1")
    client = client_factory(project_role="viewer")
    assert client.get("/api/v1/projects/proj-1").json()["effective_role"] == "viewer"
    assert client.delete("/api/v1/projects/proj-1").status_code == 403
    project_svc.delete_project.assert_not_called()


def test_the_hint_costs_no_extra_role_store_read(client_factory, role_svc):
    """Derived from the ALREADY-RESOLVED ``ProjectContext`` — a second store read would be a
    second source of truth for the same question."""
    _governed(role_svc, "proj-1")
    client = client_factory(project_role="owner")
    assert client.get("/api/v1/projects/proj-1").status_code == 200
    role_svc.has_role_rows.assert_not_called()
    role_svc.list_all_strict.assert_not_called()


def test_platform_viewer_still_403s_before_any_project_logic(client_factory, role_svc):
    """The project role is checked IN ADDITION to the platform role, never instead:
    a Platform.Viewer is refused by ``require_role(OPERATOR)`` even holding project OWNER."""
    client = client_factory(platform_role="viewer", project_role="owner")
    assert client.delete("/api/v1/projects/proj-1").status_code == 403
    role_svc.has_role_rows.assert_not_called()


# ===========================================================================
# The multi-ECS-task cache window (E27 fix pass, review I2/I6)
#
# ``ProjectResolver.invalidate()`` is process-local and ``ecs_desired_count`` is 2, so the
# creator-OWNER row written on task A is INVISIBLE to task B's cached context for up to a TTL.
# The §3 fallback cannot rescue it: ``has_role_rows`` is a LIVE read, so the moment the row
# exists the project reads as GOVERNED on every task and the fallback is correctly withheld —
# so task B hard-403s the creator on the project they just made.
#
# These tests model task B: the cached ctx carries NO role (``project_role=None``) while the
# STORE holds the caller's OWNER row. The fake resolver's ``refresh_project`` reads the same
# seeded ``_role_svc`` the real one would.
# ===========================================================================

# The caller's oid as ``_claims_for`` mints it for a platform operator.
_CALLER_OID = "operator-oid"


def _own_row(pid="proj-1", role="owner"):
    """A role row for the CALLER themselves (not the ``someone-else-oid`` of ``_governed_row``)."""
    return _governed_row(pid=pid, principal=_CALLER_OID, role=role)


def _stale_cache_client(client_factory, role_svc, *, rows, pid="proj-1"):
    """A client whose cached ctx is EMPTY while the store holds ``rows`` — i.e. the second ECS
    task, mid-window. ``has_role_rows`` is live, so the project reads as governed."""
    role_svc.has_role_rows.side_effect = lambda p: p == pid
    role_svc.list_for_project_strict.side_effect = (
        lambda p: [r for r in rows if r.project_id == p]
    )
    role_svc.list_all_strict.return_value = list(rows)
    return client_factory(project_role=None)


def test_a_stale_cache_does_not_403_the_creator_of_their_own_project(
    client_factory, role_svc, project_svc
):
    """THE headline flow. Creator holds OWNER in the store; this task's cache predates it.
    Before the fix this was a hard 403 on the project they had just created."""
    client = _stale_cache_client(client_factory, role_svc, rows=[_own_row()])
    assert client.delete("/api/v1/projects/proj-1").status_code == 204
    project_svc.delete_project.assert_called_once_with("proj-1")


def test_a_stale_cache_does_not_hide_the_promote_or_roster_gates(client_factory, role_svc):
    """The STRICT gate (no §3 fallback) takes the same fresh confirmation — otherwise the
    epic's headline OWNER-only verbs stay refused for a TTL."""
    client = _stale_cache_client(client_factory, role_svc, rows=[_own_row()])
    assert client.get("/api/v1/projects/proj-1/roles").status_code == 200


def test_the_detail_hint_reports_the_role_the_fresh_read_found(client_factory, role_svc):
    """I6: the hint must agree with the gate. Reporting ``None`` here is exactly the D18
    failure mode — a caller who genuinely holds OWNER loses Grant/Delete/Promote for a TTL."""
    client = _stale_cache_client(client_factory, role_svc, rows=[_own_row()])
    body = client.get("/api/v1/projects/proj-1").json()
    assert body["effective_role"] == "owner"
    assert body["ungoverned"] is False


def test_the_list_and_the_detail_agree_during_the_window(client_factory, role_svc, project_svc):
    """I6 stated as the property that was violated: the LIST route read the store while the
    detail hint read the cache, so the two disagreed for up to a TTL about the same project."""
    project_svc.list_projects.return_value = [_project("proj-1")]
    client = _stale_cache_client(client_factory, role_svc, rows=[_own_row()])
    assert [p["id"] for p in client.get("/api/v1/projects").json()] == ["proj-1"]
    assert client.get("/api/v1/projects/proj-1").json()["effective_role"] == "owner"


def test_a_stale_cache_never_widens_beyond_what_the_store_says(client_factory, role_svc):
    """The refresh is ADDITIVE, not permissive: a MAINTAINER row stays a maintainer, so the
    OWNER verb is still refused. The fix must not become a way to acquire authority."""
    client = _stale_cache_client(
        client_factory, role_svc, rows=[_own_row(role="maintainer")]
    )
    assert client.post(
        "/api/v1/projects/proj-1/repos", json=_valid_repo_body()
    ).status_code == 202
    r = client.delete("/api/v1/projects/proj-1")
    assert r.status_code == 403
    assert r.json()["detail"] == INSUFFICIENT


def test_a_caller_with_no_row_at_all_is_still_refused(client_factory, role_svc, project_svc):
    """The refusal must survive the extra read when the store genuinely holds nothing for the
    caller — the fresh read is a confirmation, not an escape hatch."""
    client = _stale_cache_client(client_factory, role_svc, rows=[_governed_row()])
    assert client.delete("/api/v1/projects/proj-1").status_code == 403
    project_svc.delete_project.assert_not_called()


def test_the_confirmation_read_only_happens_on_the_deny_path(client_factory, role_svc):
    """A caller who already holds the role must pay NOTHING — the fresh read is deny-path only,
    so the hot path keeps its cache."""
    _governed(role_svc, "proj-1")
    client = client_factory(project_role="owner")
    assert client.get("/api/v1/projects/proj-1").status_code == 200
    role_svc.list_for_project_strict.assert_not_called()


def test_an_unreadable_partition_leaves_the_refusal_standing(client_factory, role_svc):
    """Fails CLOSED: if the confirmation read itself faults, the original 403 stands (and is
    never laundered into a 500)."""
    from services.project_role_service import ProjectRoleError

    role_svc.has_role_rows.side_effect = lambda p: True
    role_svc.list_for_project_strict.side_effect = ProjectRoleError(
        "boom", kind="ownership_unverified"
    )
    client = client_factory(project_role=None)
    r = client.delete("/api/v1/projects/proj-1")
    assert r.status_code == 403
    assert r.json()["detail"] == INSUFFICIENT


# ===========================================================================
# Role-row cleanup on project delete (E27 fix pass, review I1)
# ===========================================================================

def test_deleting_a_project_deletes_its_role_rows(client_factory, role_svc, project_svc):
    """Otherwise ``pk="project_role"`` grows forever with rows for projects that no longer
    exist — on the partition the resolver full-scans on every cache miss — and a reused id
    would revive them as live grants."""
    _governed(role_svc, "proj-1")
    client = client_factory(project_role="owner")
    assert client.delete("/api/v1/projects/proj-1").status_code == 204
    role_svc.revoke_all.assert_called_once_with("proj-1")


def test_the_role_rows_are_deleted_only_AFTER_the_project_is_gone(
    client_factory, role_svc, project_svc
):
    """Ordering matters: stripping the owners of a project that then fails to delete would
    leave a LIVE project nobody can administer."""
    order = []
    project_svc.delete_project.side_effect = lambda pid: order.append("project")
    role_svc.revoke_all.side_effect = lambda pid: order.append("roles") or 1
    _governed(role_svc, "proj-1")
    client = client_factory(project_role="owner")
    assert client.delete("/api/v1/projects/proj-1").status_code == 204
    assert order == ["project", "roles"]


def test_a_refused_project_delete_keeps_its_role_rows(client_factory, role_svc, project_svc):
    """A 409 (repos still exist) is not a delete — the governance rows must survive it."""
    from services.project_service import ProjectError

    project_svc.delete_project.side_effect = ProjectError("nope", kind="has_repositories")
    _governed(role_svc, "proj-1")
    client = client_factory(project_role="owner")
    assert client.delete("/api/v1/projects/proj-1").status_code == 409
    role_svc.revoke_all.assert_not_called()


def test_a_failed_role_cleanup_does_not_fail_the_completed_delete(
    client_factory, role_svc, project_svc
):
    """E23 cascade idiom: the project is already gone, so raising would 500 a delete that
    SUCCEEDED and invite a retry that cannot help. Log and continue."""
    role_svc.revoke_all.side_effect = RuntimeError("ddb down")
    _governed(role_svc, "proj-1")
    client = client_factory(project_role="owner")
    assert client.delete("/api/v1/projects/proj-1").status_code == 204
    project_svc.delete_project.assert_called_once_with("proj-1")


# ===========================================================================
# Promote — the E27A error mapping (T7)
#
# E27A narrowed promote onto the PROD CANDIDATE, so the service gained
# ``kind="no_prod_candidate"``. Unmapped, it fell through the ladder's bare ``raise`` and
# left the route as a 500 — "nothing to promote" (a normal, expected state: nothing has
# merged to ``main`` since the last release) presented to the operator as a server fault,
# which the FE cannot tell apart from a real outage. It must be a 409.
# ===========================================================================

NO_CANDIDATE = "no prod candidate to promote"
_PROMOTE = "/api/v1/projects/proj-1/repos/r-1/promote"


def test_no_prod_candidate_is_409_not_500(client_factory, role_svc, project_svc):
    """The mapping itself: a repo with nothing pending is a CONFLICT, not a 500."""
    from services.project_service import ProjectError

    project_svc.promote_repo.side_effect = ProjectError(
        "no prod candidate to promote", kind="no_prod_candidate"
    )
    _governed(role_svc, "proj-1")
    client = client_factory(project_role="owner")
    r = client.post(_PROMOTE)
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == NO_CANDIDATE


def test_no_prod_candidate_detail_is_the_fixed_literal_not_the_error_message(
    client_factory, role_svc, project_svc
):
    """Ladder idiom: FIXED detail per ``.kind``, never ``str(err)`` — otherwise a future
    service message (or a store/CodeBuild one) reaches the browser verbatim."""
    from services.project_service import ProjectError

    project_svc.promote_repo.side_effect = ProjectError(
        "ddb said: table agp-repos throttled on sk=REPO#r-1", kind="no_prod_candidate"
    )
    _governed(role_svc, "proj-1")
    client = client_factory(project_role="owner")
    r = client.post(_PROMOTE)
    assert r.status_code == 409
    assert r.json()["detail"] == NO_CANDIDATE


def test_a_non_owner_still_cannot_promote(client_factory, role_svc, project_svc):
    """The REGRESSION guard on the mapping change: promote's STRICT OWNER gate is untouched.
    A MAINTAINER is refused with the fixed literal and the service is never reached — so the
    409 above can never be read as "the gate now lets everyone through to a friendlier
    error"."""
    _governed(role_svc, "proj-1")
    client = client_factory(project_role="maintainer")
    r = client.post(_PROMOTE)
    assert r.status_code == 403
    assert r.json()["detail"] == INSUFFICIENT
    project_svc.promote_repo.assert_not_called()  # refused BEFORE the deploy
