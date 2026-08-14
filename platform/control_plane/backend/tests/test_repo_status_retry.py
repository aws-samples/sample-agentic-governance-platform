"""E25C/T3 — repo materialize STATUS + RETRY endpoints (resume from failed step).

``GET  /projects/{id}/repos/{repo_id}/status`` → 200, the live ``Repository`` record with
its per-step ``steps[]`` timeline (tenant-gated via ``_load_visible_project`` first; None →
404). ``POST /projects/{id}/repos/{repo_id}/retry`` → 202: resets the failed step (+ any
non-``done`` step after it) back to ``pending``, keeps ``done`` steps ``done``, flips
``cicd_status``→``provisioning``, RE-DERIVES + re-stashes the materialize inputs from durable
state (the T2 success/failure path POPS the stash — a failed repo has NONE), then schedules
``run_materialize`` as a BackgroundTask (resume via its existing done-skip loop).

Route tests use a FastAPI TestClient over the REAL in-memory ProjectService (``table_name=""``)
with the rollout/identity/registry collaborators mocked — the same entra-mock auth harness as
``test_projects_routes.py`` (patched ``verify_entra_token`` + a fixed TenantContext). The
service-level tests drive ``retry_materialize`` / ``run_materialize`` directly to pin the
re-derivation + resume path (the whole point of T3).
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.project_service import ProjectService

FIXED = datetime(2026, 7, 2, tzinfo=timezone.utc)

VALID_AGENT_CONFIG = {
    "agent_name": "p1_agent",
    "framework": "strands",
    "model_id": "us.anthropic.claude-sonnet-4-6",
}


# --------------------------------------------------------------------------- #
# Auth harness (mirrors test_projects_routes.py)
# --------------------------------------------------------------------------- #


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


# --------------------------------------------------------------------------- #
# Collaborator mocks + real in-memory ProjectService
# --------------------------------------------------------------------------- #


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
    agent = SimpleNamespace(
        id="agent-1",
        agent_arn=None,
        name="p1_agent",
        framework="strands",
        model_id="us.anthropic.claude-sonnet-4-6",
    )
    registry = MagicMock()
    registry.create.return_value = agent
    # retry_materialize re-derives the agent from the registry by agent_id (the stash is
    # gone on a failed repo) — so registry.get must return the governed agent record.
    registry.get.return_value = agent

    identity = MagicMock()
    identity.provision_identity.return_value = None  # non-awaitable → driven as-is

    conn = MagicMock()
    conn.get_connection.return_value = SimpleNamespace(org="acme", base_url=None)
    conn.get_bearer_token.return_value = "ghp_secret_token"

    rollout = MagicMock()
    rollout.create_repo.return_value = "https://github.com/acme/my-agent"

    tenants = _FakeTenantService(_STAGES)
    return SimpleNamespace(
        registry=registry, identity=identity, conn=conn, rollout=rollout, tenants=tenants
    )


@pytest.fixture
def scaffold_dir(tmp_path_factory):
    """A real on-disk template scaffold — E28B's ``push_template`` reads it from disk."""
    root = tmp_path_factory.mktemp("agent-templates")
    (root / "strands-agentcore").mkdir()
    (root / "strands-agentcore" / "src").mkdir()
    (root / "strands-agentcore" / "src" / "main.py").write_bytes(b"# agent\n")
    return root


@pytest.fixture
def project_service(mocks, scaffold_dir):
    ids = iter([f"id-{i}" for i in range(200)])
    return ProjectService(
        table_name="",  # in-memory local fallback
        agent_templates_dir=str(scaffold_dir),
        registry=mocks.registry,
        identity=mocks.identity,
        connection_service=mocks.conn,
        github_repo_service=mocks.rollout,
        tenant_service=mocks.tenants,
        repo_vars={"AWS_REGION": "us-east-1"},
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
    import api.routes.projects as projects_module
    import api.routes.users as users_module

    projects_module._svc = project_service
    # Scoped context: sees tenant "default" (seeded/failed repos), NOT "ten-2"
    # (other_tenant_repo) — so a foreign project 404s at _load_visible_project.
    users_module._tenant_resolver = _FakeResolver(_context(tenant_ids=["default"]))
    # E27/T4 — seed the ONE project-resolver singleton too. Every gated route now resolves
    # ``get_project_ctx``, and unseeded that builds a REAL ProjectResolver + GraphService and
    # reaches login.microsoftonline.com once per request. GLOBAL, because this file pins the
    # PRE-E27 status/retry contract (``may()`` short-circuits True, so those assertions keep
    # testing what they were written to test); per-project authz is pinned in
    # ``test_projects_role_gating.py``.
    users_module._project_resolver = _FakeResolver(_project_context(is_global=True))

    app = FastAPI()
    app.include_router(projects_module.router, prefix="/api/v1")
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
    """A terminal-failed repo: ``set_repo_vars`` failed, the stash was POPPED (T2 drops it on
    terminal failure). mint_identity/create_repo/push_template are done.

    E28B/T3: the injection moved from ``create_environment`` to ``set_repo_vars``. The old point is
    a method materialize no longer calls, so a side_effect there would never fire and every
    assertion below would pass vacuously. ``set_repo_vars`` keeps the shape that matters here — a
    failure with EARLIER steps already done, which is exactly what resume has to skip past."""
    mocks.rollout.set_ci_vars.side_effect = RuntimeError("403 Forbidden")
    proj = _seed_project(project_service)
    repo = _add_repo(project_service, proj.id)
    project_service.run_materialize(repo.id)  # fails at set_repo_vars; stash popped
    # Heal the collaborator so the background retry run can succeed cleanly.
    mocks.rollout.set_ci_vars.side_effect = None
    return project_service.get_repo(repo.id)


@pytest.fixture
def half_done_repo(project_service, mocks):
    """create_repo/push_template done, set_repo_vars pending, stash PRESENT (re-derived via
    retry) — the input to the resume-skip test."""
    mocks.rollout.set_ci_vars.side_effect = RuntimeError("boom")
    proj = _seed_project(project_service)
    repo = _add_repo(project_service, proj.id)
    project_service.run_materialize(repo.id)  # fails at set_repo_vars; stash popped
    mocks.rollout.set_ci_vars.side_effect = None
    project_service.retry_materialize(repo.id)  # re-derives + re-stashes, resets failed step
    mocks.rollout.reset_mock()  # clear call history so the resume assertions are clean
    return project_service.get_repo(repo.id)


# --------------------------------------------------------------------------- #
# The 4 brief tests
# --------------------------------------------------------------------------- #


def test_status_returns_current_steps(client, seeded_repo):
    with patch("core.security_entra.verify_entra_token", return_value=_operator_claims()):
        r = client.get(
            f"/api/v1/projects/{seeded_repo.project_id}/repos/{seeded_repo.id}/status",
            headers=_headers(),
        )
    assert r.status_code == 200
    # D-B2's five, plus E28C/T5's `provision_langfuse` — six, not eight.
    assert len(r.json()["steps"]) == 6


def test_status_foreign_project_404(client, other_tenant_repo):
    with patch("core.security_entra.verify_entra_token", return_value=_operator_claims()):
        r = client.get(
            f"/api/v1/projects/{other_tenant_repo.project_id}/repos/{other_tenant_repo.id}/status",
            headers=_headers(),
        )
    assert r.status_code == 404


def test_retry_resets_failed_steps_and_returns_202(client, failed_repo):
    with patch("core.security_entra.verify_entra_token", return_value=_operator_claims()):
        r = client.post(
            f"/api/v1/projects/{failed_repo.project_id}/repos/{failed_repo.id}/retry",
            headers=_headers(),
        )
    assert r.status_code == 202
    steps = {s["key"]: s["status"] for s in r.json()["steps"]}
    assert steps["mint_identity"] == "done"  # already-done preserved
    assert steps["push_template"] == "done"  # the tree write is NOT re-run on resume
    assert steps["set_repo_vars"] == "pending"  # was failed → reset
    assert r.json()["cicd_status"] == "provisioning"


def test_run_materialize_skips_done_steps(project_service, mocks, half_done_repo):
    """Resume must not re-run a done step. The ``push_template`` half matters most: re-pushing
    would be a second write to a repo the platform already initialized."""
    project_service.run_materialize(half_done_repo.id)
    mocks.rollout.create_repo.assert_not_called()  # create_repo already done
    mocks.rollout.commit_files.assert_not_called()  # push_template already done
    mocks.rollout.set_ci_vars.assert_called()  # set_repo_vars resumes


# --------------------------------------------------------------------------- #
# EXTRA (T3 guard): retry succeeds when the stash was cleared — the re-derivation
# path is the whole point of T3. A terminal-failed repo has NO stash; retry must
# rebuild the inputs from durable state so run_materialize resumes to ready.
# --------------------------------------------------------------------------- #


def test_retry_re_derives_inputs_when_stash_cleared(project_service, mocks):
    mocks.rollout.set_ci_vars.side_effect = RuntimeError("boom")
    proj = _seed_project(project_service)
    repo = _add_repo(project_service, proj.id)
    project_service.run_materialize(repo.id)  # fails at set_repo_vars; stash POPPED
    assert repo.id not in project_service._pending_materialize  # the T3 problem: no stash

    # Heal + retry: retry_materialize must RE-DERIVE the inputs (agent from the registry,
    # connection/tenant from the project, trunk from the project) and re-stash them.
    mocks.rollout.set_ci_vars.side_effect = None
    retried = project_service.retry_materialize(repo.id)
    assert retried.cicd_status == "provisioning"
    assert project_service._pending_materialize.get(repo.id) is not None

    # run_materialize resumes from the reset step and reaches terminal success.
    project_service.run_materialize(repo.id)
    final = project_service.get_repo(repo.id)
    assert all(s.status == "done" for s in final.steps)
    assert final.cicd_status == "ready"
    assert final.repo_url == "https://github.com/acme/my-agent"  # persisted url survives


# --------------------------------------------------------------------------- #
# FIX guards (review-3): retry on an ALL-done repo must NOT strand it at
# "provisioning"; retry on an unknown repo must 404.
# --------------------------------------------------------------------------- #


@pytest.fixture
def ready_repo(project_service):
    """A fully materialized (all-done, cicd_status="ready") repo — the double-click /
    stale-UI retry target. run_materialize drives every step to done and the stash is
    popped, exactly the state a second retry POST would hit."""
    proj = _seed_project(project_service)
    repo = _add_repo(project_service, proj.id)
    project_service.run_materialize(repo.id)  # all 8 steps → done, cicd_status → ready
    return project_service.get_repo(repo.id)


def test_retry_on_ready_repo_409_and_not_stranded(client, ready_repo):
    # Pre-condition: the repo is genuinely all-done/ready (guards against a broken fixture).
    assert ready_repo.cicd_status == "ready"
    assert all(s.status == "done" for s in ready_repo.steps)

    with patch("core.security_entra.verify_entra_token", return_value=_operator_claims()):
        r = client.post(
            f"/api/v1/projects/{ready_repo.project_id}/repos/{ready_repo.id}/retry",
            headers=_headers(),
        )
    # 409 "nothing to retry" (not 202) — and crucially the repo is NOT flipped to
    # "provisioning" (the pre-fix unconditional flip would have stranded it there forever,
    # since run_materialize's done-skip loop would never re-run finalize back to "ready").
    assert r.status_code == 409
    # Read the persisted record back — it must be UNCHANGED (still ready/all-done).
    from api.routes.projects import get_project_service

    reread = get_project_service().get_repo(ready_repo.id)
    assert reread.cicd_status == "ready"
    assert all(s.status == "done" for s in reread.steps)


def test_retry_unknown_repo_404(client, seeded_repo):
    with patch("core.security_entra.verify_entra_token", return_value=_operator_claims()):
        r = client.post(
            f"/api/v1/projects/{seeded_repo.project_id}/repos/does-not-exist/retry",
            headers=_headers(),
        )
    assert r.status_code == 404
