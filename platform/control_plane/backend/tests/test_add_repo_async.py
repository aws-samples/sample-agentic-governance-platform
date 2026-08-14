"""E25C/T2 — async materialize: sync validate → 202 → per-step BackgroundTask writes.

``add_repo`` now does ONLY the sync work (validate → pre-register agent → persist a
Repository with a full ``pending`` step timeline + ``cicd_status="provisioning"``) and
returns BEFORE any of the side-effecting steps run.

E28B/T3: the step list is FIVE (D-B2) and the collaborator calls are the portable seam's
(``create_repo``/``commit_files``/``set_ci_vars``). The failure injection moved from
``create_environment`` — a method the path no longer calls, so injecting there would test
nothing — to ``commit_files``, the one tree write materialize makes. ``run_materialize(repo_id)`` runs
those steps in the background, writing each ``StepState`` (running → done/failed) via
``_save_repo_step``; on failure it marks the step failed, flips the record to failed, and
STOPS — never raising (it runs after the HTTP response).

Reuses the in-memory ProjectService fallback (``table_name=""``) with the rollout/identity/
registry collaborators mocked, matching ``test_project_service`` patterns.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from services.project_service import ProjectService

FIXED = datetime(2026, 7, 2, tzinfo=timezone.utc)

VALID_AGENT_CONFIG = {
    "agent_name": "p1_agent",
    "framework": "strands",
    "model_id": "us.anthropic.claude-sonnet-4-6",
}


def _principal(oid="O1", email="e@x"):
    return SimpleNamespace(oid=oid, email=email)


class _FakeTenantService:
    """TenantService double — ``get`` returns a fixed Tenant-like object with dev/prod
    stages (only ``.stages`` is read by the materialize steps)."""

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
    registry = MagicMock()
    registry.create.return_value = SimpleNamespace(id="agent-1", agent_arn=None, name="p1_agent")

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
    """A real on-disk template scaffold — ``push_template`` reads it via
    ``collect_scaffold_files``, so an empty/absent dir would fail the step."""
    root = tmp_path_factory.mktemp("agent-templates")
    (root / "strands-agentcore").mkdir()
    (root / "strands-agentcore" / "src").mkdir()
    (root / "strands-agentcore" / "src" / "main.py").write_bytes(b"# agent\n")
    (root / "strands-agentcore" / "Dockerfile").write_bytes(b"FROM python:3.11-slim\n")
    return root


@pytest.fixture
def project_service(mocks, scaffold_dir):
    ids = iter(["proj-1", "repo-1", "repo-2", "repo-3"])
    svc = ProjectService(
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
    svc.create_project(
        name="p1",
        connection_id="c1",
        tenant_id="default",
        description="the first project",
        created_by="op@x",
    )
    return svc


def valid_add_repo_kwargs():
    return dict(
        project_id="proj-1",
        name="my-agent",
        template_name="strands-agentcore",
        agent_config=VALID_AGENT_CONFIG,
        created_by="op@x",
        principal=_principal(),
    )


def test_add_repo_returns_record_before_side_effects(project_service, mocks):
    repo = project_service.add_repo(**valid_add_repo_kwargs())
    assert repo.cicd_status == "provisioning"
    assert [s.status for s in repo.steps] == ["pending"] * 6  # D-B2 five + D-C5's langfuse step
    mocks.rollout.create_repo.assert_not_called()  # deferred to run_materialize
    mocks.rollout.commit_files.assert_not_called()
    mocks.identity.provision_identity.assert_not_called()


def test_run_materialize_advances_steps_to_done(project_service, mocks):
    repo = project_service.add_repo(**valid_add_repo_kwargs())
    project_service.run_materialize(repo.id)
    reloaded = project_service.get_repo(repo.id)  # existing read path
    assert all(s.status == "done" for s in reloaded.steps)
    assert reloaded.cicd_status == "ready"
    # The side effects now ran (deferred from add_repo).
    mocks.identity.provision_identity.assert_called_once()
    mocks.rollout.create_repo.assert_called_once()
    mocks.rollout.commit_files.assert_called_once()


def test_run_materialize_marks_failed_step_and_stops(project_service, mocks):
    # Injected on ``commit_files`` — the one tree write. ``create_environment`` (the old injection
    # point) is no longer called at all, so a side_effect there would never fire and this test
    # would silently assert nothing.
    mocks.rollout.commit_files.side_effect = RuntimeError("403 Forbidden")
    repo = project_service.add_repo(**valid_add_repo_kwargs())
    project_service.run_materialize(repo.id)  # must NOT raise
    reloaded = project_service.get_repo(repo.id)
    failed = [s for s in reloaded.steps if s.status == "failed"]
    assert len(failed) == 1 and failed[0].key == "push_template"
    assert failed[0].error and "token" not in failed[0].error.lower()
    # steps after the failed one stayed pending
    assert reloaded.steps[-1].status == "pending"
    assert reloaded.cicd_status == "failed"
    assert reloaded.status == "failed"


def test_input_validation_still_raises_before_202(project_service, mocks):
    with pytest.raises(ValueError):
        project_service.add_repo(
            **{**valid_add_repo_kwargs(), "agent_config": {"framework": "not-strands"}}
        )
    mocks.rollout.create_repo.assert_not_called()
    mocks.registry.create.assert_not_called()


# --------------------------------------------------------------------------- #
# Resume / done-skip path (E25C/T2 fixes) — the branch T3's retry builds on.
# --------------------------------------------------------------------------- #


def test_run_materialize_skips_done_steps_and_preserves_repo_url(project_service, mocks):
    """A resumed run must NOT re-call collaborators for already-``done`` steps, and must
    NOT wipe the persisted ``repo_url`` when the ``create_repo`` step is skipped."""
    repo = project_service.add_repo(**valid_add_repo_kwargs())
    # First run: full materialize → all steps done, repo_url persisted.
    project_service.run_materialize(repo.id)
    reloaded = project_service.get_repo(repo.id)
    assert reloaded.repo_url == "https://github.com/acme/my-agent"
    assert all(s.status == "done" for s in reloaded.steps)

    # Re-stash the inputs (a real T3 retry re-derives them; the success run dropped them)
    # and re-invoke: every step is already done, so NO collaborator should be re-called.
    project_service._pending_materialize[repo.id] = {
        "agent": mocks.registry.create.return_value,
        "name": "my-agent",
        "connection_id": "c1",
        "template_name": "strands-agentcore",
        "agent_config": VALID_AGENT_CONFIG,
        "repo_overrides": None,
        "tenant_id": "default",
        "trunk_branch": "main",
    }
    mocks.identity.provision_identity.reset_mock()
    mocks.rollout.reset_mock()

    project_service.run_materialize(repo.id)

    mocks.identity.provision_identity.assert_not_called()
    mocks.rollout.create_repo.assert_not_called()
    mocks.rollout.commit_files.assert_not_called()
    mocks.rollout.set_ci_vars.assert_not_called()
    # repo_url is preserved (finalize did not overwrite it with None from empty state).
    final = project_service.get_repo(repo.id)
    assert final.repo_url == "https://github.com/acme/my-agent"
    assert final.cicd_status == "ready"


def test_run_materialize_clears_stale_error_on_recovered_step(project_service, mocks):
    """A previously-``failed`` step, on re-run to ``done``, has its ``error`` cleared."""
    mocks.rollout.commit_files.side_effect = RuntimeError("403 Forbidden")
    repo = project_service.add_repo(**valid_add_repo_kwargs())
    project_service.run_materialize(repo.id)  # push_template fails, error is set.
    failed = project_service.get_repo(repo.id)
    dev = next(s for s in failed.steps if s.key == "push_template")
    assert dev.status == "failed" and dev.error

    # The transient error clears; re-stash + re-run → the recovered step is done, no error.
    mocks.rollout.commit_files.side_effect = None
    project_service._pending_materialize[repo.id] = {
        "agent": mocks.registry.create.return_value,
        "name": "my-agent",
        "connection_id": "c1",
        "template_name": "strands-agentcore",
        "agent_config": VALID_AGENT_CONFIG,
        "repo_overrides": None,
        "tenant_id": "default",
        "trunk_branch": "main",
    }
    project_service.run_materialize(repo.id)

    recovered = project_service.get_repo(repo.id)
    dev = next(s for s in recovered.steps if s.key == "push_template")
    assert dev.status == "done"
    assert dev.error is None
    assert all(s.status == "done" for s in recovered.steps)
    assert recovered.cicd_status == "ready"
    # NEW-2 guard: on this resumed run ``create_repo`` is already done+skipped, so the
    # step ``state`` is empty and ``finalize`` receives repo_url=None. The persisted url
    # (stamped by create_repo on the first run) MUST survive — this asserts the
    # ``if repo_url is not None`` guard in ``_finalize_repo``. Reverting that guard would
    # overwrite repo_url with None here and fail this assertion.
    assert recovered.repo_url == "https://github.com/acme/my-agent"
