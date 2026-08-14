"""Projects (container) + Repositories route tests (Epic 20, Task T10).

Exercises the REAL require_role/current_principal path against a mocked
verify_entra_token (no live Entra) and a FAKE ProjectService patched onto the
router module's shared ``_svc`` (no live AWS / GitHub). Mirrors
test_ops_templates_routes.py for the app/client + role auth-override idiom.

A Project is now an EMPTY CONTAINER; materializing a repo+agent happens on
``POST /projects/{id}/repos`` (add_repo). The flat ``/repositories`` list has its
own router (``repositories_router``) in the same module.

E24/T6 — projects are tenant-scoped. The pre-T6 tests here run with an always-
GLOBAL tenant context (mirrors what T5 did to test_agents_routes_rbac.py) so the
pre-E24 behavior is preserved; the tenant-scoping contract itself is pinned in
the "E24/T6 tenant scoping" section below with explicit non-global contexts.
"""

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from models.project import Project, ProjectDetail
from models.repository import (
    Repository,
    RepoDeleteItemResult,
    RepoDeletePreview,
    RepoDeletePreviewItem,
    RepoDeleteResult,
)
from services.agent_registry_service import NameTakenError
from services.project_service import ProjectError

# THE SAME ATTACK CORPUS THE SERVICE SUITE USES, imported rather than re-listed (E28C/T4 review,
# F5). One regex governs both layers, so two hand-maintained lists could only drift into the HTTP
# suite being the weaker of the two — which it was: it dropped five strings, including
# ``../backend/src/core`` (the read that actually harvested `config.py` in E28B's repro) and
# ``strands-agentcore/../../etc`` (starts legitimate, escapes later). Cross-test-module import
# follows the existing precedent at test_repo_provider.py:42.
from test_project_service import _TRAVERSAL_NAMES


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


def _context(*, is_global=False, tenant_ids=()):
    from services.tenant_resolver import TenantContext

    return TenantContext(is_global=is_global, tenant_ids=frozenset(tenant_ids), tenants=())


class _FakeResolver:
    """Async ``resolve`` stub returning a fixed context regardless of principal. Shaped for
    BOTH resolvers — the tenant one only needs ``resolve``; the project one also gets the
    ``invalidate`` cache-drop the role writes call."""

    def __init__(self, ctx):
        self._ctx = ctx
        self.invalidate = MagicMock()

    async def resolve(self, principal):
        return self._ctx


class _FakeTenantService:
    """Minimal ``TenantService``-shaped fake: only ``.get`` is exercised by create()."""

    def __init__(self, known_ids):
        self._known = set(known_ids)

    def get(self, tenant_id):
        from services.tenant_service import TenantError

        if tenant_id not in self._known:
            raise TenantError("Unknown tenant", kind="not_found")
        return MagicMock(id=tenant_id)


def _seed_tenant_service(known_ids):
    """Patch ``tenants._svc`` (imported lazily by the create route)."""
    import api.routes.tenants as tenants_module

    tenants_module._svc = _FakeTenantService(known_ids)


def _global_project_context():
    from services.project_resolver import ProjectContext

    return ProjectContext(is_global=True, roles={})


def _build_client(fake_svc, ctx=None):
    import api.routes.projects as projects_module
    import api.routes.users as users_module

    # Patch the lazy ProjectService singleton onto BOTH routers' shared _svc so no
    # live AWS/GitHub is ever touched. reset_modules re-imports projects fresh each test.
    projects_module._svc = fake_svc

    # E27/T3 — seed the per-project role-service singleton too. create_project's
    # creator-OWNER bootstrap calls it, and its (correct) bare `except` would SWALLOW a live
    # DDB PutItem: with a real PROJECTS_TABLE_NAME in the environment these unit tests would
    # silently reach for AWS and still pass. A MagicMock keeps the bootstrap offline; the
    # role-row reads return []/False so nothing here depends on a MagicMock's truthiness
    # (the gate reads the STRICT pair; the degrading pair is seeded for the same reason).
    projects_module._role_svc = MagicMock()
    projects_module._role_svc.has_role_rows.return_value = False
    projects_module._role_svc.list_all_strict.return_value = []
    projects_module._role_svc.list_for_project.return_value = []
    projects_module._role_svc.list_all.return_value = []

    # E24/T6 — seed the ONE tenant-resolver singleton (users._tenant_resolver) with a
    # fixed context; pre-T6 tests default to GLOBAL (bypasses filtering, preserving the
    # pre-E24 behavior — same fixup T5 applied to test_agents_routes_rbac.py) and to an
    # always-known "default" tenant for creates.
    users_module._tenant_resolver = _FakeResolver(ctx or _context(is_global=True))
    # E27/T4 — and the ONE project-resolver singleton, which every gated route now reaches
    # through ``projects.get_project_ctx``. Unseeded it would build a REAL ProjectResolver +
    # GraphService, so this is also what keeps these tests offline. Pre-E27 tests default to
    # a GLOBAL project context: ``may()`` short-circuits True, preserving the pre-E27
    # behavior these cases were written to pin — exactly the fixup E24/T6 applied above for
    # the tenant gate. The per-route role thresholds themselves are pinned in
    # ``test_projects_role_gating.py``, with explicitly non-global contexts.
    users_module._project_resolver = _FakeResolver(_global_project_context())
    _seed_tenant_service(known_ids={"default", "ten-1", "ten-2"})

    app = FastAPI()
    app.include_router(projects_module.router, prefix="/api/v1")
    app.include_router(projects_module.repositories_router, prefix="/api/v1")
    return TestClient(app)


def _claims_for(role: str):
    role_app = {
        "viewer": "Platform.Viewer",
        "operator": "Platform.Operator",
        "admin": "Platform.Admin",
    }[role]
    return {"oid": f"{role}-oid", "preferred_username": f"{role}@x.com", "roles": [role_app]}


def _headers():
    return {"Authorization": "Bearer fake-token"}


def _project(id="p1", tenant_id="default"):
    return Project(
        id=id,
        name="Fraud Ops",
        connection_id="c1",
        tenant_id=tenant_id,
        description="a container",
        created_by="operator@x.com",
        created_at="2026-07-08T00:00:00+00:00",
        updated_at="2026-07-08T00:00:00+00:00",
    )


def _repository(id="r1", project_id="p1"):
    return Repository(
        id=id,
        project_id=project_id,
        name="fraud-agent",
        repo_url="https://github.com/acme/fraud-agent",
        agent_id="a1",
        template_name="strands-agentcore",
        cicd_status="provisioning",
        status="provisioning",
        created_by="operator@x.com",
        created_at="2026-07-08T00:00:00+00:00",
        updated_at="2026-07-08T00:00:00+00:00",
    )


def _agent_config():
    return {"framework": "strands", "agent_name": "fraud_agent"}


# --- RBAC: every endpoint requires OPERATOR (403 for a viewer) --------------

def test_create_project_viewer_forbidden(entra_settings):
    s = MagicMock()
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.post(
            "/api/v1/projects",
            json={"name": "Fraud Ops", "connection_id": "c1", "tenant_id": "default"},
            headers=_headers(),
        )
    assert resp.status_code == 403
    s.create_project.assert_not_called()


def test_list_projects_viewer_forbidden(entra_settings):
    s = MagicMock()
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get("/api/v1/projects", headers=_headers())
    assert resp.status_code == 403
    s.list_projects.assert_not_called()


def test_get_project_viewer_forbidden(entra_settings):
    s = MagicMock()
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get("/api/v1/projects/p1", headers=_headers())
    assert resp.status_code == 403
    s.get_project.assert_not_called()


def test_add_repo_viewer_forbidden(entra_settings):
    s = MagicMock()
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.post(
            "/api/v1/projects/p1/repos",
            json={"name": "fraud-agent", "template_name": "strands-agentcore", "agent_config": _agent_config()},
            headers=_headers(),
        )
    assert resp.status_code == 403
    s.add_repo.assert_not_called()


def test_list_repositories_viewer_forbidden(entra_settings):
    s = MagicMock()
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get("/api/v1/repositories", headers=_headers())
    assert resp.status_code == 403
    s.list_repositories.assert_not_called()


# --- POST /projects happy path: operator create → 201, created_by from principal

def test_create_project_operator_201(entra_settings):
    s = MagicMock()
    s.create_project.return_value = _project()
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            "/api/v1/projects",
            json={
                "name": "Fraud Ops",
                "connection_id": "c1",
                "tenant_id": "default",
                "description": "a container",
            },
            headers=_headers(),
        )
    assert resp.status_code == 201
    assert resp.json()["id"] == "p1"
    # created_by is taken from the principal, never a body field.
    _, kwargs = s.create_project.call_args
    assert kwargs["name"] == "Fraud Ops"
    assert kwargs["connection_id"] == "c1"
    assert kwargs["tenant_id"] == "default"
    assert kwargs["description"] == "a container"
    assert kwargs["created_by"] == "operator@x.com"


# --- E36/T15 (item 24, option B): the trunk is INTERNAL, not a create-API field ---
#
# WHAT WAS DELETED HERE, AND WHY. `ProjectCreate.trunk_branch` existed, was blank-validated, and was
# forwarded by the route — and a SECOND validator then refused every value except `"main"`, because
# the shipped agent template's workflow pins `on.push.branches: [main]` (`build.yml`), so a project
# on any other trunk materialized, reported `ready`, and then never built. Four tests pinned that
# arrangement: two that the route forwarded the field, one that a non-`main` value was a 422, one
# that a blank was a 422. All four verified CEREMONY — a field, a validator refusing everything but
# the field's own default, and a 422 for asking. The field and both validators are gone;
# `Project.trunk_branch` stays internal, so the materialize push and the prod-candidate gate still
# read a field rather than a `main` literal.
#
# THE TWO BELOW PIN THE NARROWING ITSELF: the route must state no trunk (so a half-plumbed field
# cannot re-grow), and a stale client that still sends one must get a no-op — not a 422, not a 500.


def test_create_project_states_no_trunk(entra_settings):
    """The route passes NO ``trunk_branch``; the model default is the only source of one.

    Asserted as ABSENCE FROM THE CALL rather than as a value: ``ProjectService.create_project`` no
    longer accepts the kwarg, so a route that still forwarded one would ``TypeError`` against the
    real service while the MagicMock here swallowed it silently. This is the test that reddens if
    the forwarding is restored without the (removed) service parameter."""
    s = MagicMock()
    s.create_project.return_value = _project()
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            "/api/v1/projects",
            json={"name": "Fraud Ops", "connection_id": "c1", "tenant_id": "default"},
            headers=_headers(),
        )
    assert resp.status_code == 201
    _, kwargs = s.create_project.call_args
    assert "trunk_branch" not in kwargs, (
        "the create API carries no trunk since E36/T15 — the service takes no such kwarg, so "
        f"forwarding one would TypeError against the real ProjectService; got {sorted(kwargs)}"
    )


@pytest.mark.parametrize("sent", ["release", "   "])
def test_create_project_ignores_a_trunk_branch_a_stale_client_sends(sent, entra_settings):
    """A removed field is IGNORED, not rejected — the deliberate cost of narrowing a live API.

    ``ProjectCreate`` is a plain ``BaseModel``, so Pydantic's default ``extra="ignore"`` drops the
    unknown key. Both values here used to be 422s (the template-pin validator and the blank guard);
    both are now ordinary 201s whose trunk comes from ``Project.trunk_branch``. Pinned because both
    alternatives are regressions: ``extra="forbid"`` would 422 every client still sending the old
    field, and a 500 would mean the route is still reading ``body.trunk_branch``."""
    s = MagicMock()
    s.create_project.return_value = _project()
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            "/api/v1/projects",
            json={
                "name": "Fraud Ops",
                "connection_id": "c1",
                "tenant_id": "default",
                "trunk_branch": sent,
            },
            headers=_headers(),
        )
    assert resp.status_code == 201, resp.text
    _, kwargs = s.create_project.call_args
    assert "trunk_branch" not in kwargs, sorted(kwargs)


# --- GET /projects list -----------------------------------------------------

def test_list_projects_operator_ok(entra_settings):
    s = MagicMock()
    s.list_projects.return_value = [_project()]
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.get("/api/v1/projects", headers=_headers())
    assert resp.status_code == 200
    assert resp.json()[0]["id"] == "p1"


# --- GET /projects/{id}: 404 when absent, 200 ProjectDetail when present ----

def test_get_project_404_when_absent(entra_settings):
    s = MagicMock()
    s.get_project.return_value = None
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.get("/api/v1/projects/nope", headers=_headers())
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Project not found"


def test_get_project_200_detail(entra_settings):
    s = MagicMock()
    s.get_project.return_value = ProjectDetail(
        project=_project(), repositories=[_repository()]
    )
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.get("/api/v1/projects/p1", headers=_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["project"]["id"] == "p1"
    assert body["repositories"][0]["id"] == "r1"


# --- POST /projects/{id}/repos happy path: 201, created_by from principal ---

def test_add_repo_operator_201(entra_settings):
    s = MagicMock()
    s.add_repo.return_value = _repository()
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            "/api/v1/projects/p1/repos",
            json={"name": "fraud-agent", "template_name": "strands-agentcore", "agent_config": _agent_config()},
            headers=_headers(),
        )
    # E25C/T2: async materialize — the route returns 202 (accepted) with the pending record
    # and schedules run_materialize as a BackgroundTask.
    assert resp.status_code == 202
    assert resp.json()["id"] == "r1"
    _, kwargs = s.add_repo.call_args
    assert kwargs["project_id"] == "p1"
    assert kwargs["name"] == "fraud-agent"
    assert kwargs["template_name"] == "strands-agentcore"
    assert kwargs["agent_config"] == _agent_config()
    assert kwargs["created_by"] == "operator@x.com"
    # The validated principal is forwarded so the service can default the sponsor.
    assert kwargs["principal"].oid == "operator-oid"
    # The 8 background steps are scheduled on the returned record's id.
    s.run_materialize.assert_called_once_with("r1")


# --- POST /projects/{id}/repos threads repo_overrides through to the service ---

def test_add_repo_forwards_repo_overrides(entra_settings):
    s = MagicMock()
    s.add_repo.return_value = _repository()
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            "/api/v1/projects/p1/repos",
            json={
                "name": "fraud-agent",
                "template_name": "strands-agentcore",
                "agent_config": _agent_config(),
                "repo_overrides": {"ACCOUNT_ID": "123"},
            },
            headers=_headers(),
        )
    assert resp.status_code == 202  # E25C/T2 async materialize
    # The Class-B repo overrides reach the service (pydantic no longer drops them).
    _, kwargs = s.add_repo.call_args
    assert kwargs["repo_overrides"] == {"ACCOUNT_ID": "123"}


# --- add_repo error mapping: NameTakenError → 409 -----------------------------

def test_add_repo_name_taken_409(entra_settings):
    s = MagicMock()
    s.add_repo.side_effect = NameTakenError("Agent name 'my_agent' is already in use")
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            "/api/v1/projects/p1/repos",
            json={"name": "fraud-agent", "template_name": "strands-agentcore", "agent_config": _agent_config()},
            headers=_headers(),
        )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "Agent name 'my_agent' is already in use"


# --- add_repo error mapping: not_found → 404 (FIXED detail) -----------------

def test_add_repo_not_found_404(entra_settings):
    s = MagicMock()
    s.add_repo.side_effect = ProjectError("secret internals", kind="not_found")
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            "/api/v1/projects/nope/repos",
            json={"name": "fraud-agent", "template_name": "strands-agentcore", "agent_config": _agent_config()},
            headers=_headers(),
        )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Project not found"
    assert "secret internals" not in resp.text


# E25C/T2: add_repo now does ONLY sync work and raises exactly one ProjectError kind
# ("not_found" → 404). Materialize failures surface in the background run_materialize as a
# failed record, never as a raised ProjectError here — so the old "materialize_error → 502"
# route mapping (and its test) was removed as dead.


# --- add_repo agent_config ValueError → 400 (curated safe literal) ----------

def test_add_repo_bad_agent_config_400(entra_settings):
    s = MagicMock()
    s.add_repo.side_effect = ValueError("unsupported agent_config framework 'langchain'")
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            "/api/v1/projects/p1/repos",
            json={"name": "fraud-agent", "template_name": "strands-agentcore", "agent_config": {"framework": "langchain"}},
            headers=_headers(),
        )
    assert resp.status_code == 400
    assert "unsupported agent_config framework" in resp.json()["detail"]


# --------------------------------------------------------------------------- #
# E28C/T4 — P-B5: ``template_name`` is validated at the ``add_repo`` BOUNDARY, and the route
# answers 422.
#
# WHERE THE VALIDATION USED TO BE, AND WHY THAT WAS THE BUG. E28B closed a real path traversal by
# validating ``template_name`` inside ``_resolve_scaffold_dir`` — which runs in ``push_template``,
# STEP 3 OF 5. So a request carrying ``../../etc`` was refused, but only AFTER an Entra identity had
# been minted and a repository created in the customer's org: two irreversible side effects for a
# request that was never going to succeed, and an operator left to clean up a half-materialized
# agent. The traversal was closed; the ORDER was not.
#
# DRIVEN THROUGH THE HTTP ROUTE WITH A REAL SERVICE, deliberately. E28B's review finding C-1 was
# exactly this: a service-only test passed while the route mapped the refusal to a 500. A MagicMock
# service cannot express "no agent was registered" either — the assertion that matters here is about
# what did NOT happen, so the collaborators have to be real enough to record it.
# --------------------------------------------------------------------------- #


def _real_service_with_project(tmp_path):
    """A REAL ``ProjectService`` holding one in-memory project, with recording collaborators.

    Real because P-B5's claim is "refused BEFORE the identity mint and the repo creation", and only
    a real ``add_repo`` running against recording fakes can show those calls were never made."""
    from services.project_service import ProjectService

    registry = MagicMock()
    registry.create.return_value = MagicMock(id="agent-1", agent_arn=None, name="fraud_agent")
    identity = MagicMock()
    conn = MagicMock()
    conn.get_connection.return_value = MagicMock(org="acme", base_url=None)
    conn.get_bearer_token.return_value = "ghp_secret"
    github = MagicMock()

    svc = ProjectService(
        table_name="",
        registry=registry,
        identity=identity,
        connection_service=conn,
        github_repo_service=github,
        agent_templates_dir=str(tmp_path),
    )
    svc.create_project(
        name="Fraud Ops",
        connection_id="c1",
        tenant_id="default",
        description="a container",
        created_by="operator@x.com",
    )
    project_id = svc.list_projects()[0].id
    return svc, project_id, registry, github


@pytest.mark.parametrize("bad", _TRAVERSAL_NAMES)
def test_add_repo_refuses_a_bad_template_name_with_422(entra_settings, tmp_path, bad):
    """422, not 400 and not 500: the body is syntactically valid but one field's VALUE is
    unprocessable, which is what 422 means and what the frontend's field-level error rendering
    keys off."""
    svc, project_id, _registry, _github = _real_service_with_project(tmp_path)
    client = _build_client(svc)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            f"/api/v1/projects/{project_id}/repos",
            json={"name": "fraud-agent", "template_name": bad, "agent_config": _agent_config()},
            headers=_headers(),
        )
    assert resp.status_code == 422, (bad, resp.status_code, resp.text)


def test_add_repo_refuses_a_bad_template_name_BEFORE_any_side_effect(entra_settings, tmp_path):
    """THE POINT OF P-B5. No Entra identity is minted and no repository is created.

    Both are irreversible in the customer's tenancy/org, and both used to happen before the refusal.
    Asserted as "these collaborators were never called", because a test on the status code alone
    would pass against an implementation that refused at step 3 exactly as before."""
    svc, project_id, registry, github = _real_service_with_project(tmp_path)
    client = _build_client(svc)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            f"/api/v1/projects/{project_id}/repos",
            json={
                "name": "fraud-agent",
                "template_name": "../../etc/passwd",
                "agent_config": _agent_config(),
            },
            headers=_headers(),
        )
    assert resp.status_code == 422, resp.text
    registry.create.assert_not_called()  # no governed agent pre-registered
    github.create_repo.assert_not_called()  # no repository in the customer's org
    svc._identity.provision_identity.assert_not_called()  # no Entra identity minted
    # And no repository RECORD was persisted, so the console shows no phantom half-materialized row.
    assert svc.list_repositories() == [], svc.list_repositories()


def test_add_repo_422_detail_never_discloses_the_container_layout(entra_settings, tmp_path):
    """The refusal reaches the client as a FIXED literal — the route's rule for every
    ``ProjectError`` (never ``str(err)``, which could carry a store or provider message).

    So a caller probing for the image's filesystem layout learns nothing: not a resolved absolute
    path, not the templates directory. The name they sent is already theirs to know."""
    svc, project_id, _registry, _github = _real_service_with_project(tmp_path)
    client = _build_client(svc)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            f"/api/v1/projects/{project_id}/repos",
            json={
                "name": "fraud-agent",
                "template_name": "../../etc/passwd",
                "agent_config": _agent_config(),
            },
            headers=_headers(),
        )
    assert resp.status_code == 422
    assert str(tmp_path) not in resp.text
    assert resp.json()["detail"] == "invalid template name"


def test_add_repo_still_accepts_a_LEGAL_template_name(entra_settings, tmp_path):
    """The negative of the guard, so "refuse a traversal" cannot quietly become "refuse
    everything" — a validator that rejected every name would make all the tests above pass."""
    svc, project_id, registry, _github = _real_service_with_project(tmp_path)
    client = _build_client(svc)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            f"/api/v1/projects/{project_id}/repos",
            json={
                "name": "fraud-agent",
                "template_name": "strands-agentcore",
                "agent_config": _agent_config(),
            },
            headers=_headers(),
        )
    assert resp.status_code == 202, resp.text
    registry.create.assert_called_once()


# --- GET /repositories flat list --------------------------------------------

def test_list_repositories_operator_ok(entra_settings):
    s = MagicMock()
    s.list_repositories.return_value = [_repository(), _repository(id="r2", project_id="p2")]
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.get("/api/v1/repositories", headers=_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert [r["id"] for r in body] == ["r1", "r2"]


# ===========================================================================
# E24/T6 tenant scoping — list filter, 404-byte-identical detail, create
# validation (400/403), repo routes gate through the PARENT project's tenant
# ===========================================================================


def _detail(project):
    return ProjectDetail(project=project, repositories=[])


def test_projects_list_non_admin_sees_only_own_tenant(entra_settings):
    s = MagicMock()
    s.list_projects.return_value = [
        _project(id="own", tenant_id="ten-1"),
        _project(id="foreign", tenant_id="ten-2"),
    ]
    client = _build_client(s, ctx=_context(tenant_ids=["ten-1"]))
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.get("/api/v1/projects", headers=_headers())
    assert resp.status_code == 200
    assert [p["id"] for p in resp.json()] == ["own"]


def test_projects_list_admin_sees_all(entra_settings):
    s = MagicMock()
    s.list_projects.return_value = [
        _project(id="p1", tenant_id="ten-1"),
        _project(id="p2", tenant_id="ten-2"),
    ]
    client = _build_client(s, ctx=_context(is_global=True))
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.get("/api/v1/projects", headers=_headers())
    assert resp.status_code == 200
    assert {p["id"] for p in resp.json()} == {"p1", "p2"}


def test_project_detail_foreign_tenant_404_matches_truly_missing_body(entra_settings):
    s = MagicMock()
    s.get_project.side_effect = lambda id: (
        _detail(_project(id="foreign", tenant_id="ten-2")) if id == "foreign" else None
    )
    client = _build_client(s, ctx=_context(tenant_ids=["ten-1"]))
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        foreign_resp = client.get("/api/v1/projects/foreign", headers=_headers())
        missing_resp = client.get("/api/v1/projects/truly-missing", headers=_headers())
    assert foreign_resp.status_code == 404
    assert missing_resp.status_code == 404
    assert foreign_resp.json() == missing_resp.json()


def test_project_detail_own_tenant_visible(entra_settings):
    s = MagicMock()
    s.get_project.return_value = _detail(_project(id="own", tenant_id="ten-1"))
    client = _build_client(s, ctx=_context(tenant_ids=["ten-1"]))
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.get("/api/v1/projects/own", headers=_headers())
    assert resp.status_code == 200
    assert resp.json()["project"]["id"] == "own"


def test_project_create_requires_tenant_id_422(entra_settings):
    s = MagicMock()
    client = _build_client(s, ctx=_context(tenant_ids=["ten-1"]))
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            "/api/v1/projects",
            json={"name": "Fraud Ops", "connection_id": "c1"},
            headers=_headers(),
        )
    assert resp.status_code == 422
    s.create_project.assert_not_called()


def test_project_create_unknown_tenant_400(entra_settings):
    s = MagicMock()
    client = _build_client(s, ctx=_context(tenant_ids=["ten-1"]))
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            "/api/v1/projects",
            json={"name": "Fraud Ops", "connection_id": "c1", "tenant_id": "ten-unknown"},
            headers=_headers(),
        )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "unknown tenant"
    s.create_project.assert_not_called()


def test_project_create_foreign_tenant_403(entra_settings):
    s = MagicMock()
    client = _build_client(s, ctx=_context(tenant_ids=["ten-1"]))
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            "/api/v1/projects",
            json={"name": "Fraud Ops", "connection_id": "c1", "tenant_id": "ten-2"},
            headers=_headers(),
        )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "tenant not permitted"
    s.create_project.assert_not_called()


def test_project_create_admin_bypasses_tenant_membership(entra_settings):
    s = MagicMock()
    s.create_project.return_value = _project(id="new", tenant_id="ten-2")
    client = _build_client(s, ctx=_context(is_global=True))
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.post(
            "/api/v1/projects",
            json={"name": "Fraud Ops", "connection_id": "c1", "tenant_id": "ten-2"},
            headers=_headers(),
        )
    assert resp.status_code == 201
    s.create_project.assert_called_once()


def test_project_create_own_tenant_ok(entra_settings):
    s = MagicMock()
    s.create_project.return_value = _project(id="new", tenant_id="ten-1")
    client = _build_client(s, ctx=_context(tenant_ids=["ten-1"]))
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            "/api/v1/projects",
            json={"name": "Fraud Ops", "connection_id": "c1", "tenant_id": "ten-1"},
            headers=_headers(),
        )
    assert resp.status_code == 201
    s.create_project.assert_called_once()


def test_add_repo_foreign_tenant_404_before_service_call(entra_settings):
    """Repo-create gates through the PARENT project's tenant BEFORE any side effect:
    the 404 body is byte-identical to a truly-missing project, and svc.add_repo is
    never called (no identity minted, no repo materialized)."""
    s = MagicMock()
    s.get_project.side_effect = lambda id: (
        _detail(_project(id="foreign", tenant_id="ten-2")) if id == "foreign" else None
    )
    client = _build_client(s, ctx=_context(tenant_ids=["ten-1"]))
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        foreign_resp = client.post(
            "/api/v1/projects/foreign/repos",
            json={"name": "fraud-agent", "template_name": "strands-agentcore", "agent_config": _agent_config()},
            headers=_headers(),
        )
        missing_resp = client.post(
            "/api/v1/projects/truly-missing/repos",
            json={"name": "fraud-agent", "template_name": "strands-agentcore", "agent_config": _agent_config()},
            headers=_headers(),
        )
    assert foreign_resp.status_code == 404
    assert foreign_resp.json()["detail"] == "Project not found"
    assert missing_resp.status_code == 404
    assert foreign_resp.json() == missing_resp.json()
    s.add_repo.assert_not_called()


def test_delete_routes_foreign_tenant_404_before_side_effect(entra_settings):
    """E24 merge reconciliation: the E23 delete surfaces (repo delete-preview, repo
    delete, project delete) gate through the PARENT project's tenant like every other
    mutation — a foreign project 404s ("Project not found") BEFORE any probe/teardown,
    and the service teardown methods are never called."""
    s = MagicMock()
    s.get_project.side_effect = lambda id: (
        _detail(_project(id="foreign", tenant_id="ten-2")) if id == "foreign" else None
    )
    client = _build_client(s, ctx=_context(tenant_ids=["ten-1"]))
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        preview = client.get(
            "/api/v1/projects/foreign/repos/r1/delete-preview", headers=_headers()
        )
        repo_del = client.delete("/api/v1/projects/foreign/repos/r1", headers=_headers())
        proj_del = client.delete("/api/v1/projects/foreign", headers=_headers())
    assert preview.status_code == 404
    assert repo_del.status_code == 404
    assert proj_del.status_code == 404
    assert preview.json()["detail"] == "Project not found"
    assert repo_del.json()["detail"] == "Project not found"
    assert proj_del.json()["detail"] == "Project not found"
    s.preview_delete.assert_not_called()
    s.delete_repo.assert_not_called()
    s.delete_project.assert_not_called()


def test_delete_routes_own_tenant_reach_service(entra_settings):
    """Positive side of the merge gate: a member of the project's tenant still reaches
    the E23 delete services (gate is visibility, not a new permission)."""
    s = MagicMock()
    s.get_project.return_value = _detail(_project(id="own", tenant_id="ten-1"))
    s.delete_project.return_value = None
    client = _build_client(s, ctx=_context(tenant_ids=["ten-1"]))
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        proj_del = client.delete("/api/v1/projects/own", headers=_headers())
    assert proj_del.status_code == 204
    s.delete_project.assert_called_once_with("own")


def test_add_repo_own_tenant_ok(entra_settings):
    s = MagicMock()
    s.get_project.return_value = _detail(_project(id="own", tenant_id="ten-1"))
    s.add_repo.return_value = _repository(project_id="own")
    client = _build_client(s, ctx=_context(tenant_ids=["ten-1"]))
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.post(
            "/api/v1/projects/own/repos",
            json={"name": "fraud-agent", "template_name": "strands-agentcore", "agent_config": _agent_config()},
            headers=_headers(),
        )
    assert resp.status_code == 202  # E25C/T2 async materialize
    s.add_repo.assert_called_once()


def test_repositories_list_non_admin_filtered_by_parent_project_tenant(entra_settings):
    """Repositories carry NO tenant field — the flat list inherits visibility through
    each repo's PARENT project. A repo whose project is foreign (or gone) is filtered."""
    s = MagicMock()
    s.list_repositories.return_value = [
        _repository(id="r-own", project_id="p-own"),
        _repository(id="r-foreign", project_id="p-foreign"),
        _repository(id="r-orphan", project_id="p-gone"),
    ]
    s.list_projects.return_value = [
        _project(id="p-own", tenant_id="ten-1"),
        _project(id="p-foreign", tenant_id="ten-2"),
    ]
    client = _build_client(s, ctx=_context(tenant_ids=["ten-1"]))
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.get("/api/v1/repositories", headers=_headers())
    assert resp.status_code == 200
    assert [r["id"] for r in resp.json()] == ["r-own"]


def test_repositories_list_admin_sees_all(entra_settings):
    s = MagicMock()
    s.list_repositories.return_value = [
        _repository(id="r1", project_id="p1"),
        _repository(id="r2", project_id="p2"),
    ]
    s.list_projects.return_value = [
        _project(id="p1", tenant_id="ten-1"),
        _project(id="p2", tenant_id="ten-2"),
    ]
    client = _build_client(s, ctx=_context(is_global=True))
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("admin")):
        resp = client.get("/api/v1/repositories", headers=_headers())
    assert resp.status_code == 200
    assert {r["id"] for r in resp.json()} == {"r1", "r2"}


# --- DELETE /projects/{id}/repos/{rid}: result envelope + selection pass-through
# (E23/T6). A SYNC handler (drives the async delete_identity via asyncio.run in the
# service). Partial failure is DATA in the envelope, not an HTTP error.

def test_delete_repo_route_returns_result_envelope(entra_settings):
    s = MagicMock()
    s.delete_repo.return_value = RepoDeleteResult(
        items=[RepoDeleteItemResult(item="record", outcome="deleted")],
        record_removed=True,
    )
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.request(
            "DELETE", "/api/v1/projects/p1/repos/r1", json={"identity": False}, headers=_headers()
        )
    assert resp.status_code == 200
    assert resp.json()["record_removed"] is True
    # The selection (defaults all True) rides through with the one opt-out applied.
    _, kwargs = s.delete_repo.call_args
    assert kwargs["project_id"] == "p1"
    assert kwargs["repo_id"] == "r1"
    assert kwargs["selection"].identity is False


def test_delete_repo_route_unknown_is_404(entra_settings):
    s = MagicMock()
    s.delete_repo.side_effect = ProjectError("secret internals", kind="not_found")
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.request(
            "DELETE", "/api/v1/projects/p1/repos/nope", json={}, headers=_headers()
        )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Repository not found"
    assert "secret internals" not in resp.text


# --- GET /projects/{id}/repos/{rid}/delete-preview: reachability probe (E23/T11) ---
# READ-ONLY: returns the per-artifact state envelope; OPERATOR-gated; 404 on unknown repo.

def test_delete_preview_route_returns_preview_shape(entra_settings):
    s = MagicMock()
    s.preview_delete.return_value = RepoDeletePreview(
        items=[
            RepoDeletePreviewItem(item="github", state="present"),
            RepoDeletePreviewItem(item="runtime", state="gone"),
        ]
    )
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.get("/api/v1/projects/p1/repos/r1/delete-preview", headers=_headers())
    assert resp.status_code == 200
    body = resp.json()
    states = {i["item"]: i["state"] for i in body["items"]}
    assert states == {"github": "present", "runtime": "gone"}
    _, kwargs = s.preview_delete.call_args
    assert kwargs["project_id"] == "p1"
    assert kwargs["repo_id"] == "r1"


def test_delete_preview_route_dispatches_the_probe_OFF_the_event_loop(entra_settings):
    """E36/T8 fix 1. This handler was written `async` on the grounds that `preview_delete`
    "only probes — the runtime/image/github probes are quick reads". T8's cross-account seam
    invalidated that: the runtime probe now costs a DynamoDB tenant read, an sts:AssumeRole
    ROUND TRIP and two botocore service-model loads PER STAGE, all synchronous, and on a
    black-holed STS endpoint botocore retries with backoff before the seam converts the error.
    Blocking here stalls the whole uvicorn worker — every tenant's requests on it — not just
    this caller, and the delete modal is re-openable at will.

    The assertion is the PROPERTY, not the spelling of the dispatch: a worker thread has no
    running event loop, the loop's own thread does."""
    seen = {}

    def probe(*, project_id, repo_id):
        try:
            asyncio.get_running_loop()
            seen["on_loop"] = True
        except RuntimeError:
            seen["on_loop"] = False
        return RepoDeletePreview(items=[RepoDeletePreviewItem(item="runtime", state="present")])

    s = MagicMock()
    s.preview_delete.side_effect = probe
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.get("/api/v1/projects/p1/repos/r1/delete-preview", headers=_headers())

    assert resp.status_code == 200
    assert seen["on_loop"] is False, "the blocking cross-account probe ran on the event loop"


def test_delete_preview_route_viewer_forbidden(entra_settings):
    s = MagicMock()
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get("/api/v1/projects/p1/repos/r1/delete-preview", headers=_headers())
    assert resp.status_code == 403
    s.preview_delete.assert_not_called()


def test_delete_preview_route_unknown_is_404(entra_settings):
    s = MagicMock()
    s.preview_delete.side_effect = ProjectError("secret internals", kind="not_found")
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.get("/api/v1/projects/p1/repos/nope/delete-preview", headers=_headers())
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Repository not found"
    assert "secret internals" not in resp.text


# --- DELETE /projects/{id}: 204 on success, 409 when repos still exist (E23/T6) ---

def test_delete_project_route_204(entra_settings):
    s = MagicMock()
    s.delete_project.return_value = None
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.delete("/api/v1/projects/p1", headers=_headers())
    assert resp.status_code == 204
    _, kwargs = s.delete_project.call_args
    assert s.delete_project.call_args[0][0] == "p1" or kwargs.get("project_id") == "p1"


def test_delete_project_route_409_when_repos_exist(entra_settings):
    s = MagicMock()
    s.delete_project.side_effect = ProjectError("secret internals", kind="has_repositories")
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        resp = client.delete("/api/v1/projects/p1", headers=_headers())
    assert resp.status_code == 409
    assert resp.json()["detail"] == "Project has repositories; delete them first"
    assert "secret internals" not in resp.text
