"""Projects (container) and repositories route tests.

Exercises the real ``require_role`` / ``current_principal`` path against a mocked ``verify_entra_token``
and a fake ``ProjectService`` patched onto the router module's shared ``_svc``, on an app carrying the
one ``AppError`` handler ``main`` registers, so every service error renders its table row exactly as it
does in production. A project is an empty container; materializing a repo and agent happens on
``POST /projects/{id}/repos``. The flat ``/repositories`` list has its own router in the same module.

The pre-tenancy tests run with an always-global tenant context; the tenant-scoping contract is pinned
with explicit non-global contexts further down.
"""

import ast
import asyncio
import re
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_project_materialize import _TRAVERSAL_NAMES

from models.project import Project, ProjectDetail
from models.repository import (
    RepoDeleteItemResult,
    RepoDeletePreview,
    RepoDeletePreviewItem,
    RepoDeleteResult,
    RepoDeleteSelection,
    Repository,
)
from services.agent_registry_service import NameTakenError
from services.project_errors import ProjectError

pytestmark = pytest.mark.usefixtures("reset_modules")

PROJECT_MODULES = sorted((Path(__file__).resolve().parents[1] / "src" / "services").glob("project_*.py"))
_FAKE_TOKEN = "ghs_" + "a" * 36
_TOKEN_SHAPED = re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")


def _context(*, is_global=False, tenant_ids=()):
    from services.tenant_resolver import TenantContext

    return TenantContext(is_global=is_global, tenant_ids=frozenset(tenant_ids), tenants=())


class _FakeResolver:
    """Async ``resolve`` stub returning a fixed context regardless of principal, shaped for both
    resolvers: the project one also gets the ``invalidate`` cache drop the role writes call."""

    def __init__(self, ctx):
        self._ctx = ctx
        self.invalidate = MagicMock()

    async def resolve(self, principal):
        return self._ctx


class _FakeTenantService:
    def __init__(self, known_ids):
        self._known = set(known_ids)

    def get(self, tenant_id):
        from services.tenant_service import TenantError

        if tenant_id not in self._known:
            raise TenantError("Unknown tenant", kind="not_found")
        return MagicMock(id=tenant_id)


def _seed_tenant_service(known_ids):
    import api.routes.tenants as tenants_module

    tenants_module._svc = _FakeTenantService(known_ids)


def _global_project_context():
    from services.project_resolver import ProjectContext

    return ProjectContext(is_global=True, roles={})


def _build_client(fake_svc, ctx=None):
    """The projects, project_repos and repositories routers on an app with the ``AppError`` handler, the
    fake service on the three routers' shared ``_svc``, a MagicMock role service whose strict reads
    answer "ungoverned", and the two resolver singletons seeded global by default (the per-route role
    thresholds are pinned in ``test_projects_role_gating.py``)."""
    import api.routes.project_repos as project_repos_module
    import api.routes.projects as projects_module
    import api.routes.repositories as repositories_module
    import api.routes.users as users_module
    from core.errors import install_error_handlers

    projects_module._svc = fake_svc
    projects_module._role_svc = MagicMock()
    projects_module._role_svc.has_role_rows.return_value = False
    projects_module._role_svc.list_all_strict.return_value = []
    projects_module._role_svc.list_for_project.return_value = []
    projects_module._role_svc.list_all.return_value = []

    users_module._tenant_resolver = _FakeResolver(ctx or _context(is_global=True))
    users_module._project_resolver = _FakeResolver(_global_project_context())
    _seed_tenant_service(known_ids={"default", "ten-1", "ten-2"})

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(projects_module.router, prefix="/api/v1")
    app.include_router(project_repos_module.router, prefix="/api/v1")
    app.include_router(repositories_module.repositories_router, prefix="/api/v1")
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


_REPO_BODY = {"name": "fraud-agent", "template_name": "strands-agentcore", "agent_config": _agent_config()}


def _as_operator(client, method, path, **kwargs):
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        return getattr(client, method)(path, headers=_headers(), **kwargs)


def _delete_with_body(client, path, body):
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("operator")):
        return client.request("DELETE", path, json=body, headers=_headers())


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
        resp = client.post("/api/v1/projects/p1/repos", json=_REPO_BODY, headers=_headers())
    assert resp.status_code == 403
    s.add_repo.assert_not_called()


def test_list_repositories_viewer_forbidden(entra_settings):
    s = MagicMock()
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get("/api/v1/repositories", headers=_headers())
    assert resp.status_code == 403
    s.list_repositories.assert_not_called()


def test_create_project_operator_201(entra_settings):
    """``created_by`` is taken from the principal, never a body field."""
    s = MagicMock()
    s.create_project.return_value = _project()
    client = _build_client(s)
    resp = _as_operator(
        client,
        "post",
        "/api/v1/projects",
        json={
            "name": "Fraud Ops",
            "connection_id": "c1",
            "tenant_id": "default",
            "description": "a container",
        },
    )
    assert resp.status_code == 201
    assert resp.json()["id"] == "p1"
    _, kwargs = s.create_project.call_args
    assert kwargs["name"] == "Fraud Ops"
    assert kwargs["connection_id"] == "c1"
    assert kwargs["tenant_id"] == "default"
    assert kwargs["description"] == "a container"
    assert kwargs["created_by"] == "operator@x.com"


def test_create_project_states_no_trunk(entra_settings):
    """The route passes no ``trunk_branch``: ``ProjectService.create_project`` takes no such kwarg, so a
    route that forwarded one would ``TypeError`` against the real service while the MagicMock here
    swallowed it; asserted as absence from the call."""
    s = MagicMock()
    s.create_project.return_value = _project()
    client = _build_client(s)
    resp = _as_operator(
        client,
        "post",
        "/api/v1/projects",
        json={"name": "Fraud Ops", "connection_id": "c1", "tenant_id": "default"},
    )
    assert resp.status_code == 201
    _, kwargs = s.create_project.call_args
    assert "trunk_branch" not in kwargs, sorted(kwargs)


@pytest.mark.parametrize("sent", ["release", "   "])
def test_create_project_ignores_a_trunk_branch_a_stale_client_sends(sent, entra_settings):
    """A removed field is ignored, not rejected: ``ProjectCreate`` keeps pydantic's ``extra="ignore"``,
    so a stale client gets an ordinary 201 whose trunk comes from ``Project.trunk_branch``."""
    s = MagicMock()
    s.create_project.return_value = _project()
    client = _build_client(s)
    resp = _as_operator(
        client,
        "post",
        "/api/v1/projects",
        json={"name": "Fraud Ops", "connection_id": "c1", "tenant_id": "default", "trunk_branch": sent},
    )
    assert resp.status_code == 201, resp.text
    _, kwargs = s.create_project.call_args
    assert "trunk_branch" not in kwargs, sorted(kwargs)


def test_list_projects_operator_ok(entra_settings):
    s = MagicMock()
    s.list_projects.return_value = [_project()]
    client = _build_client(s)
    resp = _as_operator(client, "get", "/api/v1/projects")
    assert resp.status_code == 200
    assert resp.json()[0]["id"] == "p1"


def test_get_project_404_when_absent(entra_settings):
    s = MagicMock()
    s.get_project.return_value = None
    client = _build_client(s)
    resp = _as_operator(client, "get", "/api/v1/projects/nope")
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Project not found", "code": "project.not_found"}


def test_get_project_200_detail(entra_settings):
    s = MagicMock()
    s.get_project.return_value = ProjectDetail(project=_project(), repositories=[_repository()])
    client = _build_client(s)
    resp = _as_operator(client, "get", "/api/v1/projects/p1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["project"]["id"] == "p1"
    assert body["repositories"][0]["id"] == "r1"


def test_add_repo_operator_202(entra_settings):
    """The route returns 202 with the pending record and schedules ``run_materialize`` as a background
    task on the returned record's id; the validated principal is forwarded for the sponsor default."""
    s = MagicMock()
    s.add_repo.return_value = _repository()
    client = _build_client(s)
    resp = _as_operator(client, "post", "/api/v1/projects/p1/repos", json=_REPO_BODY)
    assert resp.status_code == 202
    assert resp.json()["id"] == "r1"
    _, kwargs = s.add_repo.call_args
    assert kwargs["project_id"] == "p1"
    assert kwargs["name"] == "fraud-agent"
    assert kwargs["template_name"] == "strands-agentcore"
    assert kwargs["agent_config"] == _agent_config()
    assert kwargs["created_by"] == "operator@x.com"
    assert kwargs["principal"].oid == "operator-oid"
    s.run_materialize.assert_called_once_with("r1")


def test_add_repo_forwards_repo_overrides(entra_settings):
    s = MagicMock()
    s.add_repo.return_value = _repository()
    client = _build_client(s)
    resp = _as_operator(
        client,
        "post",
        "/api/v1/projects/p1/repos",
        json={**_REPO_BODY, "repo_overrides": {"ACCOUNT_ID": "123"}},
    )
    assert resp.status_code == 202
    _, kwargs = s.add_repo.call_args
    assert kwargs["repo_overrides"] == {"ACCOUNT_ID": "123"}


def test_add_repo_name_taken_409(entra_settings):
    """The agent registry's ``name_taken`` row: 409 with the service's own sentence."""
    s = MagicMock()
    s.add_repo.side_effect = NameTakenError("Agent name 'my_agent' is already in use")
    client = _build_client(s)
    resp = _as_operator(client, "post", "/api/v1/projects/p1/repos", json=_REPO_BODY)
    assert resp.status_code == 409
    assert resp.json() == {"detail": "Agent name 'my_agent' is already in use", "code": "agent.name_taken"}


def test_add_repo_not_found_404(entra_settings):
    s = MagicMock()
    s.add_repo.side_effect = ProjectError("secret internals", kind="not_found")
    client = _build_client(s)
    resp = _as_operator(client, "post", "/api/v1/projects/nope/repos", json=_REPO_BODY)
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Project not found", "code": "project.not_found"}
    assert "secret internals" not in resp.text


def test_add_repo_ungovernable_tenant_platform_409(entra_settings):
    """The pre-registered agent's platform is derived from the owning tenant; a tenant platform with no
    governable agent label refuses with a fixed detail, so the platform name never reaches the wire."""
    s = MagicMock()
    s.add_repo.side_effect = ProjectError(
        "the owning tenant's platform cannot host a governed agent", kind="unsupported"
    )
    client = _build_client(s)
    resp = _as_operator(client, "post", "/api/v1/projects/p1/repos", json=_REPO_BODY)
    assert resp.status_code == 409
    assert resp.json()["detail"] == "the project's tenant platform cannot host a governed agent"


def test_add_repo_bad_agent_config_400(entra_settings):
    """A malformed ``agent_name`` is the service's ``invalid_agent_name`` kind: 400 carrying the model's
    sentence, which names the caller's own value and the rule."""
    s = MagicMock()
    s.add_repo.side_effect = ProjectError(
        "invalid agent_config agent_name 'my-agent'; must match ^[a-zA-Z][a-zA-Z0-9_]{0,31}$",
        kind="invalid_agent_name",
    )
    client = _build_client(s)
    resp = _as_operator(
        client,
        "post",
        "/api/v1/projects/p1/repos",
        json={**_REPO_BODY, "agent_config": {"agent_name": "my-agent"}},
    )
    assert resp.status_code == 400
    assert resp.json() == {
        "detail": "invalid agent_config agent_name 'my-agent'; must match ^[a-zA-Z][a-zA-Z0-9_]{0,31}$",
        "code": "project.invalid_agent_name",
    }


def _real_service_with_project(tmp_path):
    """A real ``ProjectService`` holding one in-memory project, with recording collaborators, so a
    refusal can be shown to happen before the identity mint and the repo creation."""
    from services.project_service import ProjectService

    registry = MagicMock()
    registry.create.return_value = MagicMock(id="agent-1", agent_arn=None, name="fraud_agent")
    identity = MagicMock()
    conn = MagicMock()
    conn.get_connection.return_value = MagicMock(org="acme", base_url=None)
    conn.get_bearer_token.return_value = "ghp_secret"
    github = MagicMock()
    github.create_repo.return_value = "https://github.com/acme/fraud-agent"

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
    """The same attack corpus the service suite uses, through the HTTP route with a real service: 422,
    the status the frontend's field-level error rendering keys off."""
    svc, project_id, _registry, _github = _real_service_with_project(tmp_path)
    client = _build_client(svc)
    resp = _as_operator(
        client, "post", f"/api/v1/projects/{project_id}/repos", json={**_REPO_BODY, "template_name": bad}
    )
    assert resp.status_code == 422, (bad, resp.status_code, resp.text)


def test_add_repo_refuses_a_bad_template_name_BEFORE_any_side_effect(entra_settings, tmp_path):
    """No Entra identity is minted, no repository is created in the customer's org and no record is
    persisted; asserted as "these collaborators were never called"."""
    svc, project_id, registry, github = _real_service_with_project(tmp_path)
    client = _build_client(svc)
    resp = _as_operator(
        client,
        "post",
        f"/api/v1/projects/{project_id}/repos",
        json={**_REPO_BODY, "template_name": "../../etc/passwd"},
    )
    assert resp.status_code == 422, resp.text
    registry.create.assert_not_called()
    github.create_repo.assert_not_called()
    svc._identity.provision_identity.assert_not_called()
    assert svc.list_repositories() == [], svc.list_repositories()


def test_add_repo_422_detail_never_discloses_the_container_layout(entra_settings, tmp_path):
    """The refusal reaches the client as the row's fixed literal, so a caller probing for the image's
    filesystem layout learns nothing."""
    svc, project_id, _registry, _github = _real_service_with_project(tmp_path)
    client = _build_client(svc)
    resp = _as_operator(
        client,
        "post",
        f"/api/v1/projects/{project_id}/repos",
        json={**_REPO_BODY, "template_name": "../../etc/passwd"},
    )
    assert resp.status_code == 422
    assert str(tmp_path) not in resp.text
    assert resp.json() == {"detail": "invalid template name", "code": "project.invalid_template_name"}


def test_add_repo_still_accepts_a_LEGAL_template_name(entra_settings, tmp_path):
    """The negative of the guard, so "refuse a traversal" cannot quietly become "refuse everything"."""
    svc, project_id, registry, _github = _real_service_with_project(tmp_path)
    client = _build_client(svc)
    resp = _as_operator(client, "post", f"/api/v1/projects/{project_id}/repos", json=_REPO_BODY)
    assert resp.status_code == 202, resp.text
    registry.create.assert_called_once()


def test_add_repo_refuses_a_non_string_model_id_at_the_edge_with_422(entra_settings, tmp_path):
    """A non-string ``model_id`` is the body model's 422 naming the field; the service is never
    reached."""
    svc, project_id, registry, _github = _real_service_with_project(tmp_path)
    spy = MagicMock(wraps=svc)
    client = _build_client(spy)
    resp = _as_operator(
        client,
        "post",
        f"/api/v1/projects/{project_id}/repos",
        json={**_REPO_BODY, "agent_config": {**_agent_config(), "model_id": 123}},
    )
    assert resp.status_code == 422, resp.text
    assert [err["loc"] for err in resp.json()["detail"]] == [["body", "agent_config", "model_id"]]
    spy.add_repo.assert_not_called()
    registry.create.assert_not_called()


def test_add_repo_refuses_an_agent_config_the_record_rejects_with_pydantics_text(entra_settings, tmp_path):
    """A body value the governed Agent record refuses (a ``purpose`` over its length cap) is the
    service's ``invalid_agent_config`` kind: 400 with pydantic's own text over the caller's value, and
    nothing is pre-registered."""
    from pydantic import ValidationError

    from models.agent import AgentCreate

    purpose = "p" * 4097
    with pytest.raises(ValidationError) as expected:
        AgentCreate(name="fraud_agent", tenant_id="default", purpose=purpose)
    svc, project_id, registry, _github = _real_service_with_project(tmp_path)
    client = _build_client(svc)
    resp = _as_operator(
        client, "post", f"/api/v1/projects/{project_id}/repos", json={**_REPO_BODY, "purpose": purpose}
    )
    assert resp.status_code == 400, resp.text
    assert resp.json() == {"detail": str(expected.value), "code": "project.invalid_agent_config"}
    registry.create.assert_not_called()


def test_add_repo_refuses_an_unknown_data_classification_with_400(entra_settings, tmp_path):
    """The same kind at the same site for the classification enum's refusal, with the enum's own text."""
    svc, project_id, registry, _github = _real_service_with_project(tmp_path)
    client = _build_client(svc)
    resp = _as_operator(
        client,
        "post",
        f"/api/v1/projects/{project_id}/repos",
        json={**_REPO_BODY, "data_classification": "secretish"},
    )
    assert resp.status_code == 400, resp.text
    assert resp.json() == {
        "detail": "'secretish' is not a valid DataClassification",
        "code": "project.invalid_data_classification",
    }
    registry.create.assert_not_called()


def test_list_repositories_operator_ok(entra_settings):
    s = MagicMock()
    s.list_repositories.return_value = [_repository(), _repository(id="r2", project_id="p2")]
    client = _build_client(s)
    resp = _as_operator(client, "get", "/api/v1/repositories")
    assert resp.status_code == 200
    assert [r["id"] for r in resp.json()] == ["r1", "r2"]


def _detail(project):
    return ProjectDetail(project=project, repositories=[])


def test_projects_list_non_admin_sees_only_own_tenant(entra_settings):
    s = MagicMock()
    s.list_projects.return_value = [
        _project(id="own", tenant_id="ten-1"),
        _project(id="foreign", tenant_id="ten-2"),
    ]
    client = _build_client(s, ctx=_context(tenant_ids=["ten-1"]))
    resp = _as_operator(client, "get", "/api/v1/projects")
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
    foreign_resp = _as_operator(client, "get", "/api/v1/projects/foreign")
    missing_resp = _as_operator(client, "get", "/api/v1/projects/truly-missing")
    assert foreign_resp.status_code == 404
    assert missing_resp.status_code == 404
    assert foreign_resp.json() == missing_resp.json()


def test_project_detail_own_tenant_visible(entra_settings):
    s = MagicMock()
    s.get_project.return_value = _detail(_project(id="own", tenant_id="ten-1"))
    client = _build_client(s, ctx=_context(tenant_ids=["ten-1"]))
    resp = _as_operator(client, "get", "/api/v1/projects/own")
    assert resp.status_code == 200
    assert resp.json()["project"]["id"] == "own"


def test_project_create_requires_tenant_id_422(entra_settings):
    s = MagicMock()
    client = _build_client(s, ctx=_context(tenant_ids=["ten-1"]))
    resp = _as_operator(client, "post", "/api/v1/projects", json={"name": "Fraud Ops", "connection_id": "c1"})
    assert resp.status_code == 422
    s.create_project.assert_not_called()


def test_project_create_unknown_tenant_is_404_tenant_not_found(entra_settings):
    """An unknown tenant renders ``TenantError``'s ``not_found`` row, the tenant domain's wire; before
    the error layer this route answered 400 "unknown tenant"."""
    s = MagicMock()
    client = _build_client(s, ctx=_context(tenant_ids=["ten-1"]))
    resp = _as_operator(
        client,
        "post",
        "/api/v1/projects",
        json={"name": "Fraud Ops", "connection_id": "c1", "tenant_id": "ten-unknown"},
    )
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Tenant not found", "code": "tenant.not_found"}
    s.create_project.assert_not_called()


def test_project_create_foreign_tenant_403(entra_settings):
    """No resource exists yet, so 403 (not 404) is correct for a tenant the caller is not a member of."""
    s = MagicMock()
    client = _build_client(s, ctx=_context(tenant_ids=["ten-1"]))
    resp = _as_operator(
        client,
        "post",
        "/api/v1/projects",
        json={"name": "Fraud Ops", "connection_id": "c1", "tenant_id": "ten-2"},
    )
    assert resp.status_code == 403
    assert resp.json() == {"detail": "tenant not permitted", "code": "tenant.not_permitted"}
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
    resp = _as_operator(
        client,
        "post",
        "/api/v1/projects",
        json={"name": "Fraud Ops", "connection_id": "c1", "tenant_id": "ten-1"},
    )
    assert resp.status_code == 201
    s.create_project.assert_called_once()


def test_add_repo_foreign_tenant_404_before_service_call(entra_settings):
    """Repo create gates through the parent project's tenant before any side effect: the 404 body is
    byte-identical to a truly missing project and the service is never called."""
    s = MagicMock()
    s.get_project.side_effect = lambda id: (
        _detail(_project(id="foreign", tenant_id="ten-2")) if id == "foreign" else None
    )
    client = _build_client(s, ctx=_context(tenant_ids=["ten-1"]))
    foreign_resp = _as_operator(client, "post", "/api/v1/projects/foreign/repos", json=_REPO_BODY)
    missing_resp = _as_operator(client, "post", "/api/v1/projects/truly-missing/repos", json=_REPO_BODY)
    assert foreign_resp.status_code == 404
    assert foreign_resp.json() == {"detail": "Project not found", "code": "project.not_found"}
    assert missing_resp.status_code == 404
    assert foreign_resp.json() == missing_resp.json()
    s.add_repo.assert_not_called()


def test_delete_routes_foreign_tenant_404_before_side_effect(entra_settings):
    """The three delete surfaces gate through the parent project's tenant like every other mutation; a
    foreign project 404s before any probe or teardown and the service methods are never called."""
    s = MagicMock()
    s.get_project.side_effect = lambda id: (
        _detail(_project(id="foreign", tenant_id="ten-2")) if id == "foreign" else None
    )
    client = _build_client(s, ctx=_context(tenant_ids=["ten-1"]))
    preview = _as_operator(client, "get", "/api/v1/projects/foreign/repos/r1/delete-preview")
    repo_del = _as_operator(client, "delete", "/api/v1/projects/foreign/repos/r1")
    proj_del = _as_operator(client, "delete", "/api/v1/projects/foreign")
    assert preview.status_code == 404
    assert repo_del.status_code == 404
    assert proj_del.status_code == 404
    assert preview.json()["detail"] == "Project not found"
    assert repo_del.json()["detail"] == "Project not found"
    assert proj_del.json() == {"detail": "Project not found", "code": "project.not_found"}
    s.preview_delete.assert_not_called()
    s.delete_repo.assert_not_called()
    s.delete_project.assert_not_called()


def test_delete_routes_own_tenant_reach_service(entra_settings):
    s = MagicMock()
    s.get_project.return_value = _detail(_project(id="own", tenant_id="ten-1"))
    s.delete_project.return_value = None
    client = _build_client(s, ctx=_context(tenant_ids=["ten-1"]))
    proj_del = _as_operator(client, "delete", "/api/v1/projects/own")
    assert proj_del.status_code == 204
    s.delete_project.assert_called_once_with("own")


def test_add_repo_own_tenant_ok(entra_settings):
    s = MagicMock()
    s.get_project.return_value = _detail(_project(id="own", tenant_id="ten-1"))
    s.add_repo.return_value = _repository(project_id="own")
    client = _build_client(s, ctx=_context(tenant_ids=["ten-1"]))
    resp = _as_operator(client, "post", "/api/v1/projects/own/repos", json=_REPO_BODY)
    assert resp.status_code == 202
    s.add_repo.assert_called_once()


def test_repositories_list_non_admin_filtered_by_parent_project_tenant(entra_settings):
    """Repositories carry no tenant field; the flat list inherits visibility through each repo's parent
    project, and a repo whose project is foreign or gone is filtered."""
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
    resp = _as_operator(client, "get", "/api/v1/repositories")
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


def test_delete_repo_route_returns_result_envelope(entra_settings):
    """Partial failure is data in the envelope, not an HTTP error; the selection rides in the DELETE
    body with the one opt-out applied, which is how the frontend sends it."""
    s = MagicMock()
    s.delete_repo.return_value = RepoDeleteResult(
        items=[RepoDeleteItemResult(item="record", outcome="deleted")], record_removed=True
    )
    client = _build_client(s)
    resp = _delete_with_body(client, "/api/v1/projects/p1/repos/r1", {"identity": False})
    assert resp.status_code == 200
    assert resp.json()["record_removed"] is True
    _, kwargs = s.delete_repo.call_args
    assert kwargs["project_id"] == "p1"
    assert kwargs["repo_id"] == "r1"
    assert kwargs["selection"].identity is False
    assert kwargs["selection"].github is True


def test_delete_repo_route_without_a_body_selects_every_step(entra_settings):
    s = MagicMock()
    s.delete_repo.return_value = RepoDeleteResult(items=[], record_removed=True)
    client = _build_client(s)
    resp = _as_operator(client, "delete", "/api/v1/projects/p1/repos/r1")
    assert resp.status_code == 200
    assert s.delete_repo.call_args.kwargs["selection"] == RepoDeleteSelection()


def test_delete_repo_route_unknown_is_404(entra_settings):
    """The service raises ``repo_not_found`` for an unknown or foreign repo; the row is 404."""
    s = MagicMock()
    s.delete_repo.side_effect = ProjectError("secret internals", kind="repo_not_found")
    client = _build_client(s)
    resp = _delete_with_body(client, "/api/v1/projects/p1/repos/nope", {})
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Repository not found", "code": "project.repo_not_found"}
    assert "secret internals" not in resp.text


def test_delete_repo_route_unreadable_record_is_409(entra_settings):
    """An unreadable registry record is refused before any step: a fixed 409, not a 500."""
    s = MagicMock()
    s.delete_repo.side_effect = ProjectError("secret internals", kind="record_unreadable")
    client = _build_client(s)
    resp = _delete_with_body(client, "/api/v1/projects/p1/repos/r1", {})
    assert resp.status_code == 409
    assert resp.json()["detail"] == "this repo's registry record can't be read — nothing was deleted"
    assert "secret internals" not in resp.text


def test_delete_preview_route_returns_preview_shape(entra_settings):
    s = MagicMock()
    s.preview_delete.return_value = RepoDeletePreview(
        items=[
            RepoDeletePreviewItem(item="github", state="present"),
            RepoDeletePreviewItem(item="runtime", state="gone"),
        ]
    )
    client = _build_client(s)
    resp = _as_operator(client, "get", "/api/v1/projects/p1/repos/r1/delete-preview")
    assert resp.status_code == 200
    states = {i["item"]: i["state"] for i in resp.json()["items"]}
    assert states == {"github": "present", "runtime": "gone"}
    _, kwargs = s.preview_delete.call_args
    assert kwargs["project_id"] == "p1"
    assert kwargs["repo_id"] == "r1"


def test_delete_preview_route_dispatches_the_probe_OFF_the_event_loop(entra_settings):
    """The runtime probe costs a tenant read, an ``sts:AssumeRole`` round trip and service-model loads
    per stage, all synchronous, so it runs on a worker thread; a worker thread has no running loop."""
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
    resp = _as_operator(client, "get", "/api/v1/projects/p1/repos/r1/delete-preview")

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
    s.preview_delete.side_effect = ProjectError("secret internals", kind="repo_not_found")
    client = _build_client(s)
    resp = _as_operator(client, "get", "/api/v1/projects/p1/repos/nope/delete-preview")
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Repository not found", "code": "project.repo_not_found"}
    assert "secret internals" not in resp.text


def test_delete_preview_route_unreadable_record_is_409(entra_settings):
    """The same ``record_unreadable`` row the delete renders: the preview used to answer a generic 502."""
    s = MagicMock()
    s.preview_delete.side_effect = ProjectError("secret internals", kind="record_unreadable")
    client = _build_client(s)
    resp = _as_operator(client, "get", "/api/v1/projects/p1/repos/r1/delete-preview")
    assert resp.status_code == 409
    assert resp.json()["detail"] == "this repo's registry record can't be read — nothing was deleted"


def test_delete_project_route_204(entra_settings):
    s = MagicMock()
    s.delete_project.return_value = None
    client = _build_client(s)
    resp = _as_operator(client, "delete", "/api/v1/projects/p1")
    assert resp.status_code == 204
    _, kwargs = s.delete_project.call_args
    assert s.delete_project.call_args[0][0] == "p1" or kwargs.get("project_id") == "p1"


def test_delete_project_route_409_when_repos_exist(entra_settings):
    s = MagicMock()
    s.delete_project.side_effect = ProjectError("secret internals", kind="has_repositories")
    client = _build_client(s)
    resp = _as_operator(client, "delete", "/api/v1/projects/p1")
    assert resp.status_code == 409
    assert resp.json() == {
        "detail": "Project has repositories; delete them first",
        "code": "project.has_repositories",
    }
    assert "secret internals" not in resp.text


def test_delete_project_route_unknown_is_404(entra_settings):
    s = MagicMock()
    s.delete_project.side_effect = ProjectError("secret internals", kind="not_found")
    client = _build_client(s)
    resp = _as_operator(client, "delete", "/api/v1/projects/p1")
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Project not found", "code": "project.not_found"}


_REPO_NOT_FOUND = (404, "Repository not found")
_IN_FLIGHT = (409, "a promotion is already in flight")
_CONFLICT = (409, "a build is already running for this agent/stage, try again shortly")
_MCP_NOT_PROMOTABLE = "MCP servers are deployed through their own pipeline and cannot be promoted"
_GATE = "no passing eval run for this candidate; promote with an override reason or run the evals"

_RETRY_KINDS = [("nothing_to_retry", 409, "Nothing to retry"), ("repo_not_found", *_REPO_NOT_FOUND)]
_PROMOTE_KINDS = [
    ("no_prod_candidate", 409, "no prod candidate to promote"),
    ("no_dev_build", 409, "no dev deployment to promote"),
    ("mcp_not_promotable", 422, _MCP_NOT_PROMOTABLE),
    ("eval_gate", 409, _GATE),
    ("promote_in_flight", *_IN_FLIGHT),
    ("conflict", *_CONFLICT),
    ("promote_failed", 502, "failed to start the promotion build"),
    ("repo_not_found", *_REPO_NOT_FOUND),
]
_ROLLBACK_KINDS = [
    ("unknown_rollback_target", 409, "no such succeeded deployment to roll back to"),
    ("mcp_not_promotable", 422, _MCP_NOT_PROMOTABLE),
    ("promote_in_flight", *_IN_FLIGHT),
    ("conflict", *_CONFLICT),
    ("rollback_failed", 502, "failed to start the rollback build"),
    ("repo_not_found", *_REPO_NOT_FOUND),
]
_BUILD_KINDS = [
    ("container_build", 422, "container repos build from CI on push"),
    ("not_ready", 409, "this repo is still being created — try again when materialize finishes"),
    ("materialize_failed", 409, "this repo's setup failed — retry the setup before building"),
    ("no_commit", 409, "repo has no commit to build yet"),
    ("promote_in_flight", *_IN_FLIGHT),
    ("conflict", *_CONFLICT),
    ("build_failed", 502, "failed to start the build"),
    ("repo_not_found", *_REPO_NOT_FOUND),
]


def _service_raising(method, kind, message):
    s = MagicMock()
    s.get_repo.return_value = _repository()
    getattr(s, method).side_effect = ProjectError(message, kind=kind)
    return s


@pytest.mark.parametrize("kind,status,detail", _RETRY_KINDS)
def test_retry_repo_renders_each_project_error_kind(entra_settings, kind, status, detail):
    s = _service_raising("retry_materialize", kind, detail)
    client = _build_client(s)
    resp = _as_operator(client, "post", "/api/v1/projects/p1/repos/r1/retry")
    assert (resp.status_code, resp.json()) == (status, {"detail": detail, "code": f"project.{kind}"})
    s.run_materialize.assert_not_called()


@pytest.mark.parametrize("kind,status,detail", _PROMOTE_KINDS)
def test_promote_repo_renders_each_project_error_kind(entra_settings, kind, status, detail):
    s = _service_raising("promote_repo", kind, detail)
    client = _build_client(s)
    resp = _as_operator(client, "post", "/api/v1/projects/p1/repos/r1/promote")
    assert (resp.status_code, resp.json()) == (status, {"detail": detail, "code": f"project.{kind}"})


@pytest.mark.parametrize("kind,status,detail", _ROLLBACK_KINDS)
def test_rollback_repo_renders_each_project_error_kind(entra_settings, kind, status, detail):
    s = _service_raising("rollback_repo", kind, detail)
    client = _build_client(s)
    resp = _as_operator(client, "post", "/api/v1/projects/p1/repos/r1/rollback", json={"image_tag": "c" * 40})
    assert (resp.status_code, resp.json()) == (status, {"detail": detail, "code": f"project.{kind}"})


@pytest.mark.parametrize("kind,status,detail", _BUILD_KINDS)
def test_build_repo_renders_each_project_error_kind(entra_settings, kind, status, detail):
    s = _service_raising("build_repo", kind, detail)
    client = _build_client(s)
    resp = _as_operator(client, "post", "/api/v1/projects/p1/repos/r1/build")
    assert (resp.status_code, resp.json()) == (status, {"detail": detail, "code": f"project.{kind}"})


@pytest.mark.parametrize(
    "method,path",
    [
        ("promote_repo", "/api/v1/projects/p1/repos/r1/promote"),
        ("rollback_repo", "/api/v1/projects/p1/repos/r1/rollback"),
        ("build_repo", "/api/v1/projects/p1/repos/r1/build"),
    ],
)
def test_fixed_detail_kinds_never_echo_the_service_message(entra_settings, method, path):
    """A fixed-detail row shows its literal whatever the service put in the message."""
    s = _service_raising(method, "conflict", f"CodeBuild said: {_FAKE_TOKEN}")
    client = _build_client(s)
    body = {"image_tag": "c" * 40} if method == "rollback_repo" else None
    resp = _as_operator(client, "post", path, **({"json": body} if body else {}))
    assert resp.status_code == 409
    assert resp.json()["detail"] == _CONFLICT[1]
    assert _FAKE_TOKEN not in resp.text


def _message_is_fixed_copy(expr: ast.expr) -> bool:
    """A string literal, a module constant by name, or an f-string whose only interpolations are a
    module regex's ``.pattern``; never a caught exception's text or a call result."""
    if isinstance(expr, ast.Constant):
        return isinstance(expr.value, str)
    if isinstance(expr, ast.Name):
        return True
    if isinstance(expr, ast.JoinedStr):
        return all(
            isinstance(part, ast.Constant)
            or (
                isinstance(part, ast.FormattedValue)
                and isinstance(part.value, ast.Attribute)
                and part.value.attr == "pattern"
            )
            for part in expr.values
        )
    return False


def _raise_sites(kind: str) -> list[ast.Call]:
    sites = []
    for module in PROJECT_MODULES:
        for node in ast.walk(ast.parse(module.read_text())):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "ProjectError"
            ):
                continue
            kinds = [kw.value for kw in node.keywords if kw.arg == "kind"]
            if kinds and isinstance(kinds[0], ast.Constant) and kinds[0].value == kind:
                sites.append(node)
    return sites


@pytest.mark.parametrize("kind", ["mcp_refused", "mcp_not_promotable", "eval_gate"])
def test_project_error_detail_for_kind_carries_no_upstream_body(entra_settings, kind):
    """The three ``Outcome(status, None)`` rows send the message verbatim, so the message must be safe
    by construction: every raise site of the kind in the service passes fixed copy (a literal, a module
    constant, or an f-string over a module regex's pattern), never a store, provider or exception text.
    The route half: the message reaches the client unchanged and carries nothing token-shaped."""
    sites = _raise_sites(kind)
    assert sites, f"no raise site for kind {kind!r} in services/project_*.py"
    for site in sites:
        assert _message_is_fixed_copy(site.args[0]), ast.unparse(site)

    s = _service_raising("promote_repo", kind, "the service's own sentence")
    s.add_repo.side_effect = s.promote_repo.side_effect
    client = _build_client(s)
    path = "/api/v1/projects/p1/repos" if kind == "mcp_refused" else "/api/v1/projects/p1/repos/r1/promote"
    resp = _as_operator(client, "post", path, json=_REPO_BODY)
    assert resp.json() == {"detail": "the service's own sentence", "code": f"project.{kind}"}
    assert not _TOKEN_SHAPED.search(resp.text)


def test_project_error_detail_for_framework_mismatch_carries_no_upstream_body(entra_settings):
    """``framework_mismatch`` sends the message verbatim, so the message must be safe by construction:
    the real validator composes it from the request's framework and the catalog's and nothing else,
    and the route renders exactly that sentence with nothing token-shaped."""
    from models.project import FrameworkMismatchError, validate_agent_config

    with pytest.raises(FrameworkMismatchError) as caught:
        validate_agent_config({**_agent_config(), "framework": "langgraph"}, "strands")
    s = MagicMock()
    s.get_repo.return_value = _repository()
    s.add_repo.side_effect = caught.value
    client = _build_client(s)

    resp = _as_operator(client, "post", "/api/v1/projects/p1/repos", json=_REPO_BODY)

    assert resp.status_code == 422
    assert resp.json() == {
        "detail": (
            "agent_config framework 'langgraph' does not match the registered template's framework 'strands'"
        ),
        "code": "project.framework_mismatch",
    }
    assert not _TOKEN_SHAPED.search(resp.text)


_MCP_BODY = {"name": "claims-api", "template_name": "gateway-agentcore", "mcp_name": "claims-tools"}


def _mcp_repository():
    return Repository(
        id="r1",
        project_id="p1",
        name="claims-api",
        artifact_kind="mcp_server",
        mcp_server_id="mcp-1",
        template_name="gateway-agentcore",
        cicd_status="provisioning",
        status="provisioning",
        created_by="operator@x.com",
        created_at="2026-07-08T00:00:00+00:00",
        updated_at="2026-07-08T00:00:00+00:00",
    )


def test_add_repo_forwards_mcp_name_and_needs_no_agent_config(entra_settings):
    s = MagicMock()
    s.add_repo.return_value = _mcp_repository()
    client = _build_client(s)
    resp = _as_operator(client, "post", "/api/v1/projects/p1/repos", json=_MCP_BODY)
    assert resp.status_code == 202
    body = resp.json()
    assert (body["artifact_kind"], body["mcp_server_id"], body["agent_id"]) == ("mcp_server", "mcp-1", None)
    _, kwargs = s.add_repo.call_args
    assert kwargs["mcp_name"] == "claims-tools"
    assert kwargs["agent_config"] == {}


@pytest.mark.parametrize(
    "message",
    [
        "MCP servers can only be built in the platform account for now",
        "this template's infrastructure cannot build an MCP server",
    ],
)
def test_add_repo_mcp_refusal_is_422_with_the_service_literal(entra_settings, message):
    s = MagicMock()
    s.add_repo.side_effect = ProjectError(message, kind="mcp_refused")
    client = _build_client(s)
    resp = _as_operator(client, "post", "/api/v1/projects/p1/repos", json=_MCP_BODY)
    assert resp.status_code == 422
    assert resp.json() == {"detail": message, "code": "project.mcp_refused"}
    s.run_materialize.assert_not_called()


def test_add_repo_mcp_name_taken_is_409(entra_settings):
    from services.mcp_server_service import NameTakenError as McpNameTakenError

    s = MagicMock()
    s.add_repo.side_effect = McpNameTakenError("MCP server name 'claims-tools' is already in use")
    client = _build_client(s)
    resp = _as_operator(client, "post", "/api/v1/projects/p1/repos", json=_MCP_BODY)
    assert resp.status_code == 409
    assert resp.json() == {
        "detail": "MCP server name 'claims-tools' is already in use",
        "code": "mcp_server.name_taken",
    }


def test_add_repo_mcp_registry_validation_is_422_not_400(entra_settings):
    """``McpValidationError`` is also a ``ValueError``; it renders its own 422 row, never a 400."""
    from services.mcp_server_service import McpValidationError

    s = MagicMock()
    s.add_repo.side_effect = McpValidationError("description too long")
    client = _build_client(s)
    resp = _as_operator(client, "post", "/api/v1/projects/p1/repos", json=_MCP_BODY)
    assert resp.status_code == 422
    assert resp.json() == {"detail": "description too long", "code": "mcp_server.schema"}


def _deployment(**over):
    from models.deployment import Deployment

    base: dict[str, Any] = dict(
        id="dep-00000001",
        repo_id="r1",
        mcp_server_id="mcp-1",
        stage="dev",
        seq_key="r1#dev#2026-07-08T00:00:00+00:00#0001",
        image_tag="c" * 40,
        started_at="2026-07-08T00:00:00+00:00",
    )
    base.update(over)
    return Deployment(**base)


DEPLOYMENTS = "/api/v1/projects/p1/repos/r1/deployments"


def test_repo_deployments_route_returns_the_repo_history(entra_settings):
    s = MagicMock()
    s.get_repo.return_value = _mcp_repository()
    s.list_deployments.return_value = [_deployment()]
    client = _build_client(s)
    resp = _as_operator(client, "get", DEPLOYMENTS + "?stage=dev")
    assert resp.status_code == 200
    rows = resp.json()
    assert [(r["id"], r["mcp_server_id"], r["agent_id"]) for r in rows] == [("dep-00000001", "mcp-1", None)]
    s.list_deployments.assert_called_once_with("r1", stage="dev", limit=50)


def test_repo_deployments_route_omitted_stage_is_cross_stage(entra_settings):
    s = MagicMock()
    s.get_repo.return_value = _repository()
    s.list_deployments.return_value = []
    client = _build_client(s)
    resp = _as_operator(client, "get", DEPLOYMENTS)
    assert resp.status_code == 200 and resp.json() == []
    s.list_deployments.assert_called_once_with("r1", stage=None, limit=50)


def test_repo_deployments_route_viewer_forbidden(entra_settings):
    s = MagicMock()
    client = _build_client(s)
    with patch("core.security_entra.verify_entra_token", return_value=_claims_for("viewer")):
        resp = client.get(DEPLOYMENTS, headers=_headers())
    assert resp.status_code == 403
    s.list_deployments.assert_not_called()


@pytest.mark.parametrize("repo", [None, "other-project"])
def test_repo_deployments_route_unknown_or_foreign_repo_is_404(entra_settings, repo):
    s = MagicMock()
    s.get_repo.return_value = None if repo is None else _repository(project_id="p2")
    client = _build_client(s)
    resp = _as_operator(client, "get", DEPLOYMENTS)
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Repository not found", "code": "project.repo_not_found"}
    s.list_deployments.assert_not_called()


def test_repo_deployments_route_foreign_tenant_404_before_the_read(entra_settings):
    s = MagicMock()
    s.get_project.return_value = _detail(_project(tenant_id="ten-2"))
    client = _build_client(s, ctx=_context(tenant_ids=["ten-1"]))
    resp = _as_operator(client, "get", DEPLOYMENTS)
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Project not found"
    s.get_repo.assert_not_called()
    s.list_deployments.assert_not_called()


PULL_REQUESTS = "/api/v1/repositories/r1/pull-requests"


def _pull_requests_with(fake_svc, links, ctx=None):
    """``GET .../pull-requests`` with the provider coordinates and the link service stubbed: the
    coordinates resolve, the caller's links are ``links``, and no provider is reached."""
    client = _build_client(fake_svc, ctx=ctx)
    link_service = MagicMock()
    link_service.list_for_principal.return_value = links
    with (
        patch("api.routes.connections.get_connection_service", return_value=MagicMock()),
        patch("api.routes.repositories.provider_coordinates", return_value=("acme", None)),
        patch("api.routes.github_link.get_github_link_service", return_value=link_service),
    ):
        return _as_operator(client, "get", PULL_REQUESTS), link_service


def test_pull_requests_without_a_github_link_is_409_with_its_code(entra_settings):
    """The refusal the route decides: the caller's AGP standing is fine, but no live link names the
    GitHub human to act as, so the UI gets the remedy and a code to key it on."""
    s = MagicMock()
    s.get_repo.return_value = _repository()
    s.get_project.return_value = _detail(_project())
    resp, link_service = _pull_requests_with(s, links=[])
    assert resp.status_code == 409
    assert resp.json() == {
        "detail": "connect your GitHub account to act on pull requests",
        "code": "github_link.not_linked",
    }
    link_service.get_user_bearer_token.assert_not_called()


@pytest.mark.parametrize("missing", ["repo", "project", "foreign"])
def test_pull_requests_on_an_absent_repo_is_404_with_its_code(entra_settings, missing):
    """A missing repo, a repo whose project is gone and a repo in a foreign tenant answer the same
    body, so the three cannot be told apart."""
    s = MagicMock()
    s.get_repo.return_value = None if missing == "repo" else _repository()
    s.get_project.return_value = None if missing == "project" else _detail(_project(tenant_id="ten-2"))
    resp, link_service = _pull_requests_with(s, links=[], ctx=_context(tenant_ids=["ten-1"]))
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Repository not found", "code": "project.repo_not_found"}
    link_service.list_for_principal.assert_not_called()
