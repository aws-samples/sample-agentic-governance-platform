"""The repo materialize status and retry endpoints: resume from the failed step.

``GET /projects/{id}/repos/{repo_id}/status`` answers 200 with the live ``Repository`` record and its
per-step ``steps[]`` timeline, tenant-gated through ``_load_visible_project`` first (``None`` is a 404).
``POST /projects/{id}/repos/{repo_id}/retry`` answers 202: it resets the failed step and any non-``done``
step after it back to ``pending``, keeps ``done`` steps ``done``, flips ``cicd_status`` to
``provisioning``, re-derives and re-stashes the materialize inputs from durable state (the success and
failure paths pop the stash, so a failed repo has none), then schedules ``run_materialize`` as a
background task, which resumes through its done-skip loop.

Route tests use a FastAPI ``TestClient`` over the real in-memory ``ProjectService`` (``table_name=""``)
with the rollout, identity and registry collaborators mocked, under the same Entra-mock auth harness as
``test_projects_routes.py`` (a patched ``verify_entra_token`` and a fixed ``TenantContext``). The
service-level tests drive ``retry_materialize`` and ``run_materialize`` directly to pin the
re-derivation and resume path.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.project_service import ProjectService

pytestmark = pytest.mark.usefixtures("reset_modules")

FIXED = datetime(2026, 7, 2, tzinfo=timezone.utc)

VALID_AGENT_CONFIG = {
    "agent_name": "p1_agent",
    "framework": "strands",
    "model_id": "us.anthropic.claude-sonnet-4-6",
}


def _context(*, is_global=False, tenant_ids=()):
    from services.tenant_resolver import TenantContext

    return TenantContext(is_global=is_global, tenant_ids=frozenset(tenant_ids), tenants=())


def _project_context(*, is_global=False, roles=None):
    from services.project_resolver import ProjectContext

    return ProjectContext(is_global=is_global, roles=roles or {})


class _FakeResolver:
    def __init__(self, ctx):
        self._ctx = ctx

    async def resolve(self, principal):
        return self._ctx


def _operator_claims():
    return {
        "oid": "operator-oid",
        "preferred_username": "operator@x.com",
        "roles": ["Platform.Operator"],
    }


def _headers():
    return {"Authorization": "Bearer fake-token"}


class _FakeTenantService:
    def __init__(self, stages):
        self._stages = stages

    def get(self, tenant_id):
        return SimpleNamespace(id=tenant_id, stages=self._stages)


_STAGES = {
    "dev": SimpleNamespace(
        ecr_repo_uri="ecr-dev-uri",
        region="us-east-1",
        push_role_arn="arn:aws:iam::111111111111:role/push-dev",
    ),
    "prod": SimpleNamespace(
        ecr_repo_uri="ecr-prod-uri",
        region="eu-west-1",
        push_role_arn="arn:aws:iam::222222222222:role/push-prod",
    ),
}


@pytest.fixture
def mocks():
    """Collaborator mocks; ``registry.get`` returns the agent because ``retry_materialize`` re-derives it
    by ``agent_id`` once the stash is gone."""
    agent = SimpleNamespace(
        id="agent-1",
        agent_arn=None,
        name="p1_agent",
        framework="strands",
        model_id="us.anthropic.claude-sonnet-4-6",
    )
    registry = MagicMock()
    registry.create.return_value = agent
    registry.get.return_value = agent

    identity = MagicMock()
    identity.provision_identity.return_value = None

    conn = MagicMock()
    conn.get_connection.return_value = SimpleNamespace(org="acme", base_url=None)
    conn.get_bearer_token.return_value = "ghp_secret_token"

    rollout = MagicMock()
    rollout.create_repo.return_value = "https://github.com/acme/my-agent"

    tenants = _FakeTenantService(_STAGES)
    return SimpleNamespace(registry=registry, identity=identity, conn=conn, rollout=rollout, tenants=tenants)


@pytest.fixture
def main_only_scaffold_dir(tmp_path_factory):
    """One template of only ``src/main.py``, written per test; ``push_template`` reads it from disk."""
    root = tmp_path_factory.mktemp("agent-templates")
    (root / "strands-agentcore").mkdir()
    (root / "strands-agentcore" / "src").mkdir()
    (root / "strands-agentcore" / "src" / "main.py").write_bytes(b"# agent\n")
    return root


@pytest.fixture
def project_service(mocks, main_only_scaffold_dir):
    """An in-memory service over the mocks; ``repo_vars`` carries ``AGP_API_URL`` because it is in the
    scaffold workflows' contract and ``set_repo_vars`` refuses to write it blank."""
    ids = iter([f"id-{i}" for i in range(200)])
    return ProjectService(
        table_name="",
        agent_templates_dir=str(main_only_scaffold_dir),
        registry=mocks.registry,
        identity=mocks.identity,
        connection_service=mocks.conn,
        github_repo_service=mocks.rollout,
        tenant_service=mocks.tenants,
        repo_vars={"AWS_REGION": "us-east-1", "AGP_API_URL": "https://agp.example/api/v1"},
        new_id=lambda: next(ids),
        now=lambda: FIXED,
    )


def _seed_project(svc, tenant_id="default"):
    return svc.create_project(
        name="p1",
        connection_id="c1",
        tenant_id=tenant_id,
        description="the first project",
        created_by="op@x",
    )


def _add_repo(svc, project_id):
    return svc.add_repo(
        project_id=project_id,
        name="my-agent",
        template_name="strands-agentcore",
        agent_config=VALID_AGENT_CONFIG,
        created_by="op@x",
        principal=SimpleNamespace(oid="O1", email="e@x"),
    )


@pytest.fixture
def client(project_service, entra_settings):
    """A client over the projects and project_repos routers. The tenant context sees ``default`` and not
    ``ten-2``, so a foreign project 404s at ``_load_visible_project``. The project-resolver singleton is
    seeded too, because unseeded it builds a real ``ProjectResolver`` and ``GraphService`` that reach
    Entra once per request; it is global because this file pins the status and retry contract (``may()``
    short-circuits true) and per-project authorization is ``test_projects_role_gating.py``'s."""
    import api.routes.project_repos as project_repos_module
    import api.routes.projects as projects_module
    import api.routes.users as users_module
    from core.errors import install_error_handlers

    projects_module._svc = project_service
    users_module._tenant_resolver = _FakeResolver(_context(tenant_ids=["default"]))
    users_module._project_resolver = _FakeResolver(_project_context(is_global=True))

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(projects_module.router, prefix="/api/v1")
    app.include_router(project_repos_module.router, prefix="/api/v1")
    return TestClient(app)


@pytest.fixture
def seeded_repo(project_service):
    proj = _seed_project(project_service)
    return _add_repo(project_service, proj.id)


@pytest.fixture
def other_tenant_repo(project_service):
    proj = _seed_project(project_service, tenant_id="ten-2")
    return _add_repo(project_service, proj.id)


@pytest.fixture
def failed_repo(project_service, mocks):
    """A terminal-failed repo: ``set_repo_vars`` failed with ``mint_identity``, ``create_repo`` and
    ``push_template`` already done, and the stash popped on terminal failure, exactly the shape resume
    has to skip past; the collaborator is healed so the retry run can succeed."""
    mocks.rollout.set_ci_vars.side_effect = RuntimeError("403 Forbidden")
    proj = _seed_project(project_service)
    repo = _add_repo(project_service, proj.id)
    project_service.run_materialize(repo.id)
    mocks.rollout.set_ci_vars.side_effect = None
    return project_service.get_repo(repo.id)


@pytest.fixture
def half_done_repo(project_service, mocks):
    """``create_repo`` and ``push_template`` done, ``set_repo_vars`` pending and the stash present again
    after a retry: the input to the resume-skip test, with the mock's call history cleared."""
    mocks.rollout.set_ci_vars.side_effect = RuntimeError("boom")
    proj = _seed_project(project_service)
    repo = _add_repo(project_service, proj.id)
    project_service.run_materialize(repo.id)
    mocks.rollout.set_ci_vars.side_effect = None
    project_service.retry_materialize(repo.id)
    mocks.rollout.reset_mock()
    return project_service.get_repo(repo.id)


def test_status_returns_current_steps(client, seeded_repo):
    """Six steps: the five core materialize steps plus ``provision_langfuse``."""
    with patch("core.security_entra.verify_entra_token", return_value=_operator_claims()):
        r = client.get(
            f"/api/v1/projects/{seeded_repo.project_id}/repos/{seeded_repo.id}/status",
            headers=_headers(),
        )
    assert r.status_code == 200
    assert len(r.json()["steps"]) == 6


def test_status_foreign_project_404(client, other_tenant_repo):
    with patch("core.security_entra.verify_entra_token", return_value=_operator_claims()):
        r = client.get(
            f"/api/v1/projects/{other_tenant_repo.project_id}/repos/{other_tenant_repo.id}/status",
            headers=_headers(),
        )
    assert r.status_code == 404


def test_retry_resets_failed_steps_and_returns_202(client, failed_repo):
    """Done steps stay done, the tree write is not re-run on resume, the failed step resets to pending."""
    with patch("core.security_entra.verify_entra_token", return_value=_operator_claims()):
        r = client.post(
            f"/api/v1/projects/{failed_repo.project_id}/repos/{failed_repo.id}/retry",
            headers=_headers(),
        )
    assert r.status_code == 202
    steps = {s["key"]: s["status"] for s in r.json()["steps"]}
    assert steps["mint_identity"] == "done"
    assert steps["push_template"] == "done"
    assert steps["set_repo_vars"] == "pending"
    assert r.json()["cicd_status"] == "provisioning"


def test_run_materialize_skips_done_steps(project_service, mocks, half_done_repo):
    """Resume must not re-run a done step. The ``push_template`` half matters most: re-pushing
    would be a second write to a repo the platform already initialized."""
    project_service.run_materialize(half_done_repo.id)
    mocks.rollout.create_repo.assert_not_called()
    mocks.rollout.commit_files.assert_not_called()
    mocks.rollout.set_ci_vars.assert_called()


def test_retry_re_derives_inputs_when_stash_cleared(project_service, mocks):
    """A terminal-failed repo has no stash; retry rebuilds the inputs from durable state (the agent from
    the registry, connection, tenant and trunk from the project) and re-stashes them, so
    ``run_materialize`` resumes from the reset step to ready and the persisted url survives."""
    mocks.rollout.set_ci_vars.side_effect = RuntimeError("boom")
    proj = _seed_project(project_service)
    repo = _add_repo(project_service, proj.id)
    project_service.run_materialize(repo.id)
    assert repo.id not in project_service._pending_materialize

    mocks.rollout.set_ci_vars.side_effect = None
    retried = project_service.retry_materialize(repo.id)
    assert retried.cicd_status == "provisioning"
    assert project_service._pending_materialize.get(repo.id) is not None

    project_service.run_materialize(repo.id)
    final = project_service.get_repo(repo.id)
    assert all(s.status == "done" for s in final.steps)
    assert final.cicd_status == "ready"
    assert final.repo_url == "https://github.com/acme/my-agent"


@pytest.fixture
def ready_repo(project_service):
    """A fully materialized repo (every step done, ``cicd_status="ready"``, the stash popped): the
    double-click or stale-UI retry target."""
    proj = _seed_project(project_service)
    repo = _add_repo(project_service, proj.id)
    project_service.run_materialize(repo.id)
    return project_service.get_repo(repo.id)


def test_retry_on_ready_repo_409_and_not_stranded(client, ready_repo):
    """Retry on an all-done repo answers 409 and leaves the persisted record unchanged: an unconditional
    flip to ``provisioning`` would strand it there, since the done-skip loop never re-runs finalize back
    to ``ready``. The fixture's state is asserted first so a broken fixture cannot pass vacuously."""
    assert ready_repo.cicd_status == "ready"
    assert all(s.status == "done" for s in ready_repo.steps)

    with patch("core.security_entra.verify_entra_token", return_value=_operator_claims()):
        r = client.post(
            f"/api/v1/projects/{ready_repo.project_id}/repos/{ready_repo.id}/retry",
            headers=_headers(),
        )
    assert r.status_code == 409
    from api.routes.projects import get_project_service

    reread = get_project_service().get_repo(ready_repo.id)
    assert reread is not None
    assert reread.cicd_status == "ready"
    assert all(s.status == "done" for s in reread.steps)


def test_retry_unknown_repo_404(client, seeded_repo):
    with patch("core.security_entra.verify_entra_token", return_value=_operator_claims()):
        r = client.post(
            f"/api/v1/projects/{seeded_repo.project_id}/repos/does-not-exist/retry",
            headers=_headers(),
        )
    assert r.status_code == 404
