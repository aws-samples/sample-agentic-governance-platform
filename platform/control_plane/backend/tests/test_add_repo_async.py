"""Async materialize: ``add_repo`` does only the sync work (validate, pre-register the agent, persist a
``Repository`` with a full ``pending`` step timeline and ``cicd_status="provisioning"``) and returns before
any side-effecting step runs; ``run_materialize(repo_id)`` runs the steps in the background, writing each
``StepState`` through ``_save_repo_step``, and on failure marks the step failed, flips the record to
failed and stops without raising.

The in-memory ``ProjectService`` (``table_name=""``) with the rollout, identity and registry collaborators
mocked. The collaborator calls are the portable seam's (``create_repo``, ``commit_files``,
``set_ci_vars``) and the failure injection is on ``commit_files``, the one tree write materialize makes.
The template catalog is a real ``TemplateRegistry`` whose record is registered but not dereferenceable,
which keeps every test on the on-disk seed arm.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from services.project_service import ProjectService
from services.template_registry import TemplateRecord, TemplateRegistry, template_id_for

FIXED = datetime(2026, 7, 2, tzinfo=timezone.utc)

TEMPLATE_NAME = "strands-agentcore"

VALID_AGENT_CONFIG = {
    "agent_name": "p1_agent",
    "framework": "strands",
    "model_id": "us.anthropic.claude-sonnet-4-6",
}


def _principal(oid="O1", email="e@x"):
    return SimpleNamespace(oid=oid, email=email)


class _FakeTenantService:
    """TenantService double; ``get`` returns a Tenant-like object carrying the ``stages`` and ``platform``
    the materialize steps read."""

    def __init__(self, stages, platform="aws"):
        self._stages = stages
        self._platform = platform

    def get(self, tenant_id):
        return SimpleNamespace(id=tenant_id, stages=self._stages, platform=self._platform)


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
    identity.provision_identity.return_value = None

    conn = MagicMock()
    conn.get_connection.return_value = SimpleNamespace(org="acme", base_url=None)
    conn.get_bearer_token.return_value = "ghp_secret_token"

    rollout = MagicMock()
    rollout.create_repo.return_value = "https://github.com/acme/my-agent"

    tenants = _FakeTenantService(_STAGES)
    return SimpleNamespace(registry=registry, identity=identity, conn=conn, rollout=rollout, tenants=tenants)


@pytest.fixture
def main_and_dockerfile_scaffold_dir(tmp_path_factory):
    """One template of only ``src/main.py`` and a ``Dockerfile``, written per test: ``push_template``
    reads it via ``collect_scaffold_files``, so an empty/absent dir would fail the step."""
    root = tmp_path_factory.mktemp("agent-templates")
    (root / "strands-agentcore").mkdir()
    (root / "strands-agentcore" / "src").mkdir()
    (root / "strands-agentcore" / "src" / "main.py").write_bytes(b"# agent\n")
    (root / "strands-agentcore" / "Dockerfile").write_bytes(b"FROM python:3.11-slim\n")
    return root


@pytest.fixture
def catalog():
    """A real in-memory ``TemplateRegistry`` with this module's template registered.

    ``add_repo`` validates the framework in ``agent_config`` against the record's, so a service with no
    registry answers "framework unknown" and ``test_input_validation_still_raises_before_202`` would
    assert nothing about frameworks. ``source_org`` and ``source_repo`` stay unset: they are what makes
    a record dereferenceable, and setting them would move ``run_materialize`` from the on-disk seed arm
    every test below pins (``main_and_dockerfile_scaffold_dir`` plus ``commit_files``) to the
    read-the-template-repo arm."""
    reg = TemplateRegistry(table_name="", now=lambda: FIXED)
    reg.create(
        TemplateRecord(
            id=template_id_for(TEMPLATE_NAME),
            name=TEMPLATE_NAME,
            connection_id="c1",
            created_at=FIXED.isoformat(),
            framework="strands",
        )
    )
    return reg


@pytest.fixture
def project_service(mocks, main_and_dockerfile_scaffold_dir, catalog):
    """An in-memory service over the mocks; ``repo_vars`` carries ``AGP_API_URL`` because it is in the
    scaffold workflows' contract and ``set_repo_vars`` refuses to write it blank."""
    ids = iter(["proj-1", "repo-1", "repo-2", "repo-3"])
    svc = ProjectService(
        table_name="",
        agent_templates_dir=str(main_and_dockerfile_scaffold_dir),
        registry=mocks.registry,
        identity=mocks.identity,
        connection_service=mocks.conn,
        github_repo_service=mocks.rollout,
        tenant_service=mocks.tenants,
        template_registry=catalog,
        repo_vars={"AWS_REGION": "us-east-1", "AGP_API_URL": "https://agp.example/api/v1"},
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


def test_the_pre_registered_agents_platform_is_derived_from_the_tenant(project_service, mocks):
    """The platform stamp is derived from the tenant, not a hardcoded ``aws_bedrock``: this pre-register
    writes straight into the registry and bypasses the create/update route's platform-tenant validation,
    so a hardcoded label could contradict its own tenant with nothing downstream to catch it."""
    from models.agent import GOVERNABLE_PLATFORMS, Platform

    project_service.add_repo(**valid_add_repo_kwargs())
    created = mocks.registry.create.call_args[0][0]
    assert created.platform is Platform.AWS_BEDROCK
    assert GOVERNABLE_PLATFORMS[Platform.AWS_BEDROCK] == "aws"


def test_a_databricks_tenants_repo_agent_is_not_stamped_aws(project_service, mocks):
    """The contradiction the old stamp wrote: a Databricks-platform tenant's project
    pre-registered an ``aws_bedrock`` agent under it. The label now follows the tenant."""
    from models.agent import Platform

    project_service._tenants = _FakeTenantService(_STAGES, platform="databricks")

    project_service.add_repo(**valid_add_repo_kwargs())
    created = mocks.registry.create.call_args[0][0]
    assert created.platform is Platform.DATABRICKS


def test_add_repo_returns_record_before_side_effects(project_service, mocks):
    repo = project_service.add_repo(**valid_add_repo_kwargs())
    assert repo.cicd_status == "provisioning"
    assert [s.status for s in repo.steps] == ["pending"] * 6
    mocks.rollout.create_repo.assert_not_called()
    mocks.rollout.commit_files.assert_not_called()
    mocks.identity.provision_identity.assert_not_called()


def test_run_materialize_advances_steps_to_done(project_service, mocks):
    repo = project_service.add_repo(**valid_add_repo_kwargs())
    project_service.run_materialize(repo.id)
    reloaded = project_service.get_repo(repo.id)
    assert all(s.status == "done" for s in reloaded.steps)
    assert reloaded.cicd_status == "ready"
    mocks.identity.provision_identity.assert_called_once()
    mocks.rollout.create_repo.assert_called_once()
    mocks.rollout.commit_files.assert_called_once()


def test_run_materialize_marks_failed_step_and_stops(project_service, mocks):
    """A failing ``commit_files``, the one tree write, marks ``push_template`` failed, leaves the later
    steps pending and ends the run without raising."""
    mocks.rollout.commit_files.side_effect = RuntimeError("403 Forbidden")
    repo = project_service.add_repo(**valid_add_repo_kwargs())
    project_service.run_materialize(repo.id)
    reloaded = project_service.get_repo(repo.id)
    failed = [s for s in reloaded.steps if s.status == "failed"]
    assert len(failed) == 1 and failed[0].key == "push_template"
    assert failed[0].error and "token" not in failed[0].error.lower()
    assert reloaded.steps[-1].status == "pending"
    assert reloaded.cicd_status == "failed"
    assert reloaded.status == "failed"


def test_input_validation_still_raises_before_202(project_service, mocks):
    """Sync validation refuses before the 202 and before any side effect: ``"langgraph"`` is refused
    because this template is registered as ``strands``, not because it is unsupported, and ``agent_name``
    is valid so the framework relation is what carries the assertion."""
    with pytest.raises(ValueError, match="does not match"):
        project_service.add_repo(
            **{
                **valid_add_repo_kwargs(),
                "agent_config": {"agent_name": "p1_agent", "framework": "langgraph"},
            }
        )
    mocks.rollout.create_repo.assert_not_called()
    mocks.registry.create.assert_not_called()


def test_run_materialize_skips_done_steps_and_preserves_repo_url(project_service, mocks):
    """A resumed run re-calls no collaborator for a step already ``done`` and keeps the persisted
    ``repo_url`` when ``create_repo`` is skipped; the inputs are re-stashed by hand because a success run
    drops them and a retry re-derives them."""
    repo = project_service.add_repo(**valid_add_repo_kwargs())
    project_service.run_materialize(repo.id)
    reloaded = project_service.get_repo(repo.id)
    assert reloaded.repo_url == "https://github.com/acme/my-agent"
    assert all(s.status == "done" for s in reloaded.steps)

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
    final = project_service.get_repo(repo.id)
    assert final.repo_url == "https://github.com/acme/my-agent"
    assert final.cicd_status == "ready"


def test_run_materialize_clears_stale_error_on_recovered_step(project_service, mocks):
    """A step that failed and then runs to ``done`` has its ``error`` cleared, and ``_finalize_repo`` keeps
    the persisted ``repo_url`` when the skipped ``create_repo`` step hands it ``None``."""
    mocks.rollout.commit_files.side_effect = RuntimeError("403 Forbidden")
    repo = project_service.add_repo(**valid_add_repo_kwargs())
    project_service.run_materialize(repo.id)
    failed = project_service.get_repo(repo.id)
    dev = next(s for s in failed.steps if s.key == "push_template")
    assert dev.status == "failed" and dev.error

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
    assert recovered.repo_url == "https://github.com/acme/my-agent"


def test_mcp_template_persists_a_pending_mcp_repo_and_defers_every_side_effect(mocks):
    """A catalog record declaring ``artifact_kind="mcp_server"`` routes ``add_repo`` to the MCP
    pre-register: the MCP record is created before the 202 and the pending repo carries it; no agent, no
    identity mint and no provider call happen before the background run."""
    reg = TemplateRegistry(table_name="", now=lambda: FIXED)
    reg.create(
        TemplateRecord(
            id=template_id_for("gateway-agentcore"),
            name="gateway-agentcore",
            connection_id="c1",
            created_at=FIXED.isoformat(),
            artifact_kind="mcp_server",
            infra_template_id="agentcore-gateway",
        )
    )
    infra = MagicMock()
    infra.get.return_value = SimpleNamespace(build_kind="config", artifact_kinds=["mcp_server"])
    stages = {"dev": SimpleNamespace(account_id="111111111111", region="us-east-1")}
    mcp_registry = MagicMock()
    mcp_registry.create.return_value = SimpleNamespace(id="mcp-1", name="claims-api")
    mcp_identity = MagicMock()
    ids = iter(["proj-1", "repo-1"])
    svc = ProjectService(
        table_name="",
        registry=mocks.registry,
        identity=mocks.identity,
        connection_service=mocks.conn,
        github_repo_service=mocks.rollout,
        tenant_service=_FakeTenantService(stages),
        template_registry=reg,
        infra_template_registry=infra,
        mcp_registry=mcp_registry,
        mcp_identity=mcp_identity,
        new_id=lambda: next(ids),
        now=lambda: FIXED,
    )
    svc._platform_account_id = lambda: "111111111111"
    svc.create_project(name="p1", connection_id="c1", tenant_id="default", description="d", created_by="op@x")

    repo = svc.add_repo(
        project_id="proj-1",
        name="claims-api",
        template_name="gateway-agentcore",
        agent_config={},
        created_by="op@x",
        principal=_principal(),
    )

    assert (repo.artifact_kind, repo.mcp_server_id, repo.agent_id) == ("mcp_server", "mcp-1", None)
    assert repo.cicd_status == "provisioning"
    assert all(s.status.value == "pending" for s in repo.steps)
    assert mcp_registry.create.call_args.kwargs["repo_id"] == "repo-1"
    mocks.registry.create.assert_not_called()
    mcp_identity.mint_app.assert_not_called()
    mocks.rollout.create_repo.assert_not_called()


def _mint_sets_app_id(mocks, order):
    def _provision(agent):
        order.append("provision_identity")
        agent.entra_app_id = "app-guid"

    mocks.identity.provision_identity.side_effect = _provision


def _patch_cred(monkeypatch, order, *, boom=None):
    from unittest.mock import AsyncMock

    from services import project_materialize

    async def _ensure(agent):
        order.append("ensure_provider")
        if boom is not None:
            raise boom
        return f"agp-agent-obo-{agent.id}"

    cred = MagicMock()
    cred.ensure_agent_credential_provider = AsyncMock(side_effect=_ensure)
    monkeypatch.setattr(project_materialize, "_get_agent_credential_service", lambda: cred)
    return cred


def test_materialize_creates_the_agent_credential_provider_after_minting(project_service, mocks, monkeypatch):
    """The agent's OAuth credential provider is created inside the ``mint_identity`` step, after the Entra
    app exists, with no step key of its own; it is the agent's own credential and grants nothing, so a
    declared MCP server is reachable the moment Governance grants it."""
    order: list[str] = []
    _mint_sets_app_id(mocks, order)
    cred = _patch_cred(monkeypatch, order)

    repo = project_service.add_repo(**valid_add_repo_kwargs())
    project_service.run_materialize(repo.id)

    assert order == ["provision_identity", "ensure_provider"]
    agent = mocks.registry.create.return_value
    cred.ensure_agent_credential_provider.assert_awaited_once_with(agent)
    assert agent.oauth2_credential_provider_name == "agp-agent-obo-agent-1"
    mocks.registry.persist_identity.assert_called_once_with(agent)
    reloaded = project_service.get_repo(repo.id)
    assert reloaded.cicd_status == "ready"
    assert {s.key: s.status for s in reloaded.steps}["mint_identity"] == "done"


def test_a_credential_provider_failure_does_not_fail_materialize(project_service, mocks, monkeypatch, caplog):
    """The provider is best effort: a failure is logged and swallowed, every step stays done."""
    order: list[str] = []
    _mint_sets_app_id(mocks, order)
    _patch_cred(monkeypatch, order, boom=RuntimeError("vault down"))

    repo = project_service.add_repo(**valid_add_repo_kwargs())
    with caplog.at_level("WARNING"):
        project_service.run_materialize(repo.id)

    assert order == ["provision_identity", "ensure_provider"]
    mocks.registry.persist_identity.assert_not_called()
    reloaded = project_service.get_repo(repo.id)
    assert all(s.status == "done" for s in reloaded.steps)
    assert reloaded.cicd_status == "ready"
    assert "credential provider" in caplog.text


def test_no_credential_provider_without_an_entra_app(project_service, mocks, monkeypatch):
    """The provider's clientId IS the agent's Entra app id: no app ⇒ nothing to create (the grant
    path creates it later, get-or-create)."""
    order: list[str] = []
    cred = _patch_cred(monkeypatch, order)

    repo = project_service.add_repo(**valid_add_repo_kwargs())
    project_service.run_materialize(repo.id)

    cred.ensure_agent_credential_provider.assert_not_called()
    assert project_service.get_repo(repo.id).cicd_status == "ready"
