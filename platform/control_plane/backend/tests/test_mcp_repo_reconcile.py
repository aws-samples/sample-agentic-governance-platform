"""The ProjectService MCP-server path.

An ``mcp_server`` template materializes a governed MCP record + a repo instead of an Agent:
``add_repo`` refuses BEFORE any side effect (infra kind, name rule, platform account), then
pre-registers the MCP record carrying ``project_id``/``repo_id``; materialize mints the MCP's
Entra app, stamps ``MCP_SERVER_ID`` (never ``AGENT_ID``) and skips Langfuse; repo reads adopt a
Terraform-built gateway onto the MCP record and never fail over it. The persistence half pins the
``agent_id-index`` GSI rule: no repo OR deployment row ever carries ``agent_id: null``.

Same idiom as ``test_add_repo_async``: the real in-memory ``ProjectService`` (``table_name=""``)
with every collaborator mocked, the McpIdentity and McpServerRegistry services among them.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from seed import seed

from core import aws_clients
from models.deployment import DeploymentOutcome
from models.mcp_server import Kind
from models.project import Project
from models.repository import Repository, StepStatus
from services import project_materialize
from services.project_errors import ProjectError
from services.project_service import ProjectService
from services.project_store import _DEPLOYMENT_PK, _PROJECT_PK, _REPOSITORY_PK, _to_item

FIXED = datetime(2026, 10, 5, tzinfo=timezone.utc)
FIXED_TS = FIXED.isoformat()
PLATFORM_ACCOUNT = "111111111111"
OTHER_ACCOUNT = "222222222222"
TEMPLATE = "gateway-agentcore"
MCP_REFUSAL = "MCP servers can only be built in the platform account for now"


def _principal():
    return SimpleNamespace(oid="O1", email="e@x")


def _project():
    return Project(
        id="proj-1",
        name="Claims",
        connection_id="conn-1",
        tenant_id="t-1",
        description="a container",
        created_by="e@x",
        created_at=FIXED_TS,
        updated_at=FIXED_TS,
    )


def _tenants(account=PLATFORM_ACCOUNT):
    tenants = MagicMock()
    tenants.get.return_value = SimpleNamespace(
        id="t-1",
        platform="aws",
        stages={
            "dev": SimpleNamespace(
                account_id=account,
                region="us-east-1",
                deploy_role_arn="",
                ecr_repo_uri="",
                push_role_arn="",
            )
        },
    )
    return tenants


def _templates(artifact_kind="mcp_server"):
    reg = MagicMock()
    reg.get.return_value = SimpleNamespace(
        framework="", artifact_kind=artifact_kind, infra_template_id="agentcore-gateway"
    )
    return reg


def _infra(artifact_kinds=("mcp_server",)):
    reg = MagicMock()
    reg.get.return_value = SimpleNamespace(build_kind="config", artifact_kinds=list(artifact_kinds))
    return reg


def _mcp(**over):
    base: dict[str, Any] = dict(id="mcp-1", name="claims-api", tenant_id="t-1", gateway_arn=None)
    base.update(over)
    return SimpleNamespace(**base)


@pytest.fixture
def m():
    mcp_registry = MagicMock()
    mcp_registry.create.return_value = _mcp()
    mcp_registry.get.return_value = _mcp()
    mcp_identity = MagicMock()
    mcp_identity.mint_app.side_effect = lambda mcp: mcp
    rollout = MagicMock()
    rollout.create_repo.return_value = "https://github.com/acme/claims-api"
    conn = MagicMock()
    conn.get_connection.return_value = SimpleNamespace(org="acme", base_url=None)
    conn.get_bearer_token.return_value = "tok"
    return SimpleNamespace(
        registry=MagicMock(),
        identity=MagicMock(),
        mcp_registry=mcp_registry,
        mcp_identity=mcp_identity,
        rollout=rollout,
        conn=conn,
        langfuse=MagicMock(langfuse_host="https://lf"),
    )


def _svc(m, *, tenants=None, templates=None, infra=None, platform=PLATFORM_ACCOUNT):
    """The real in-memory service over the mocks; ``repo_vars`` carries ``AGP_API_URL`` because the gateway
    ``deploy.yml`` reads it and ``set_repo_vars`` refuses to write it blank."""
    ids = iter(["repo-1", "repo-2", "repo-3"])
    s = ProjectService(
        table_name="",
        registry=m.registry,
        identity=m.identity,
        connection_service=m.conn,
        github_repo_service=m.rollout,
        tenant_service=tenants if tenants is not None else _tenants(),
        template_registry=templates or _templates(),
        infra_template_registry=infra or _infra(),
        langfuse_provisioning=m.langfuse,
        mcp_registry=m.mcp_registry,
        mcp_identity=m.mcp_identity,
        repo_vars={"AGP_API_URL": "https://agp.example/api/v1"},
        new_id=lambda: next(ids),
        now=lambda: FIXED,
    )
    s._platform_account_id = lambda: platform
    seed(s._store, _to_item(_PROJECT_PK, _project()))
    return s


def _add(svc, **over):
    kwargs: dict[str, Any] = dict(
        project_id="proj-1",
        name="claims-api",
        template_name=TEMPLATE,
        agent_config={},
        created_by="e@x",
        principal=_principal(),
    )
    kwargs.update(over)
    return svc.add_repo(**kwargs)


def _assert_no_side_effect(m):
    m.mcp_registry.create.assert_not_called()
    m.registry.create.assert_not_called()
    m.rollout.create_repo.assert_not_called()


def test_infra_without_mcp_kind_is_refused_before_any_side_effect(m):
    svc = _svc(m, infra=_infra(artifact_kinds=("agent",)))
    with pytest.raises(ProjectError) as err:
        _add(svc)
    assert err.value.kind == "mcp_refused"
    _assert_no_side_effect(m)
    assert svc._load_all_repos() == []


@pytest.mark.parametrize("bad", ["Claims", "claims_api", "c", "1claims", "a" * 32])
def test_mcp_name_failing_the_global_rule_is_refused(m, bad):
    svc = _svc(m)
    with pytest.raises(ProjectError) as err:
        _add(svc, mcp_name=bad)
    assert err.value.kind == "mcp_refused"
    _assert_no_side_effect(m)


def test_mcp_name_defaults_to_the_repo_name_and_is_checked(m):
    svc = _svc(m)
    with pytest.raises(ProjectError) as err:
        _add(svc, name="Claims_API")
    assert err.value.kind == "mcp_refused"
    _assert_no_side_effect(m)


@pytest.mark.parametrize(
    "tenants,platform",
    [
        pytest.param(_tenants(OTHER_ACCOUNT), PLATFORM_ACCOUNT, id="another-account"),
        pytest.param(_tenants(PLATFORM_ACCOUNT), None, id="platform-account-unknown-sts-failed"),
        pytest.param(_tenants(""), PLATFORM_ACCOUNT, id="tenant-dev-account-unknown"),
    ],
)
def test_non_platform_or_unknown_account_is_refused(m, tenants, platform):
    svc = _svc(m, tenants=tenants, platform=platform)
    with pytest.raises(ProjectError) as err:
        _add(svc)
    assert err.value.kind == "mcp_refused"
    assert str(err.value) == MCP_REFUSAL
    _assert_no_side_effect(m)


def test_tenant_lookup_failure_is_refused(m):
    tenants = MagicMock()
    tenants.get.side_effect = RuntimeError("throttled")
    svc = _svc(m, tenants=tenants)
    with pytest.raises(ProjectError) as err:
        _add(svc)
    assert str(err.value) == MCP_REFUSAL
    _assert_no_side_effect(m)


def test_platform_account_probe_caches_only_a_success(m, monkeypatch):
    svc = ProjectService(
        table_name="", registry=MagicMock(), identity=MagicMock(), connection_service=MagicMock()
    )
    calls = []

    def _client(service, *, region=None):
        calls.append(service)
        if len(calls) == 1:
            raise RuntimeError("sts down")
        return MagicMock(get_caller_identity=lambda: {"Account": PLATFORM_ACCOUNT})

    monkeypatch.setattr(aws_clients, "client", _client)
    assert svc._platform_account_id() is None
    assert svc._platform_account_id() == PLATFORM_ACCOUNT
    assert svc._platform_account_id() == PLATFORM_ACCOUNT
    assert calls == ["sts", "sts"]


def test_pre_registers_a_gateway_mcp_record_owned_by_the_repo(m):
    svc = _svc(m)
    repo = _add(
        svc,
        purpose="Claims API as governed MCP tools",
        business_unit="Claims",
        region="EU",
        data_classification="Internal",
    )

    args, kwargs = m.mcp_registry.create.call_args
    req = args[0]
    assert req.name == "claims-api"
    assert req.kind is Kind.GATEWAY
    assert req.description == "Claims API as governed MCP tools"
    assert req.tenant_id == "t-1"
    assert (req.owner_oid, req.owner_email) == ("O1", "e@x")
    assert (req.business_unit, req.region) == ("Claims", "EU")
    assert req.data_classification.value == "Internal"
    assert req.gateway_arn is None
    assert kwargs["project_id"] == "proj-1"
    assert kwargs["repo_id"] == "repo-1" == repo.id
    assert kwargs["created_by"] == "e@x"

    assert repo.artifact_kind == "mcp_server"
    assert repo.mcp_server_id == "mcp-1"
    assert repo.agent_id is None
    assert repo.status == "provisioning"
    m.registry.create.assert_not_called()
    assert svc._pending_materialize["repo-1"]["mcp"].id == "mcp-1"


def test_explicit_mcp_name_and_default_description(m):
    svc = _svc(m)
    _add(svc, mcp_name="claims-tools")
    req = m.mcp_registry.create.call_args.args[0]
    assert req.name == "claims-tools"
    assert req.description == "MCP server claims-tools"


def test_long_purpose_is_cut_to_the_registry_cap(m):
    svc = _svc(m)
    _add(svc, purpose="x" * 300)
    assert len(m.mcp_registry.create.call_args.args[0].description) == 100


def test_agent_template_still_pre_registers_an_agent(m):
    m.registry.create.return_value = SimpleNamespace(id="agent-1", agent_arn=None, name="p1_agent")
    svc = _svc(m, templates=_templates(artifact_kind="agent"), infra=_infra(("agent",)))
    repo = _add(
        svc,
        agent_config={"agent_name": "p1_agent", "framework": "", "model_id": "m"},
        mcp_name="ignored",
    )
    assert repo.artifact_kind == "agent" and repo.agent_id == "agent-1"
    m.mcp_registry.create.assert_not_called()


def _materialize(svc):
    with (
        patch.object(
            project_materialize,
            "_resolve_template_bytes",
            MagicMock(return_value=({"gateway.yaml": b"x"}, "seed")),
        ),
        patch.object(project_materialize, "_adopt_trunk", MagicMock()),
    ):
        svc.run_materialize("repo-1")
    repo = svc._get_repo("repo-1")
    assert repo is not None
    return repo


def test_materialize_mints_the_mcp_app_and_skips_langfuse(m):
    svc = _svc(m)
    _add(svc)
    order = MagicMock()
    order.attach_mock(m.mcp_identity.mint_app, "mint_app")
    order.attach_mock(m.rollout.create_repo, "create_repo")
    order.attach_mock(m.rollout.set_ci_vars, "set_ci_vars")

    repo = _materialize(svc)

    assert [c[0] for c in order.mock_calls] == ["mint_app", "create_repo", "set_ci_vars"]
    m.identity.provision_identity.assert_not_called()
    m.langfuse.provision_agent_project.assert_not_called()
    assert all(s.status == StepStatus.DONE for s in repo.steps)
    langfuse = next(s for s in repo.steps if s.key == "provision_langfuse")
    assert "not used by MCP servers" in langfuse.label
    assert repo.status == "ready"


def test_set_repo_vars_stamps_mcp_server_id_not_agent_id(m):
    svc = _svc(m)
    _add(svc)
    _materialize(svc)
    ci_vars = m.rollout.set_ci_vars.call_args.args[2]
    assert ci_vars["MCP_SERVER_ID"] == "mcp-1"
    assert "AGENT_ID" not in ci_vars
    assert ci_vars["CONNECTION_ID"] == "conn-1"


def test_retry_rederives_the_mcp_inputs(m):
    svc = _svc(m)
    _add(svc)
    m.rollout.create_repo.side_effect = RuntimeError("boom")
    _materialize(svc)
    m.rollout.create_repo.side_effect = None
    svc._pending_materialize.clear()

    svc.retry_materialize("repo-1")
    m.mcp_registry.get.assert_called_with("mcp-1")
    m.registry.get.assert_not_called()
    assert svc._pending_materialize["repo-1"]["mcp"].id == "mcp-1"


def _mcp_repo(**over):
    base: dict[str, Any] = dict(
        id="repo-1",
        project_id="proj-1",
        name="claims-api",
        artifact_kind="mcp_server",
        mcp_server_id="mcp-1",
        template_name=TEMPLATE,
        status="ready",
        cicd_status="ready",
        created_by="e@x",
        created_at=FIXED_TS,
        updated_at=FIXED_TS,
    )
    base.update(over)
    return Repository(**base)


def _repo_row(svc):
    row = svc._store.get((_REPOSITORY_PK, "repo-1"))
    assert row is not None
    return row


def test_write_repo_never_sets_a_null_agent_id(m):
    svc = _svc(m)
    svc._save_repo(_mcp_repo(), include_cicd_status=True)
    row = _repo_row(svc)
    assert "agent_id" not in row
    assert row["mcp_server_id"] == "mcp-1"


def test_gateway_fields_are_never_written_by_the_backend(m):
    svc = _svc(m)
    svc._save_repo(_mcp_repo(gateway_arn="arn:gw", gateway_id="gw", gateway_url="https://gw"))
    assert not set(_repo_row(svc)) & {"gateway_arn", "gateway_id", "gateway_url"}


def test_agent_repo_still_writes_agent_id(m):
    svc = _svc(m)
    repo = _mcp_repo(artifact_kind="agent", mcp_server_id=None, agent_id="a-1")
    svc._save_repo(repo)
    assert _repo_row(svc)["agent_id"] == "a-1"


def test_mcp_deployment_row_carries_no_agent_id(m):
    svc = _svc(m)
    row = svc.append_deployment(repo_id="repo-1", mcp_server_id="mcp-1", stage="dev", image_tag="c" * 40)
    assert row.mcp_server_id == "mcp-1" and row.agent_id is None
    item = svc._store.get((_DEPLOYMENT_PK, row.seq_key))
    assert item is not None
    assert "agent_id" not in item
    assert item["mcp_server_id"] == "mcp-1"


def test_agent_deployment_row_keeps_agent_id(m):
    svc = _svc(m)
    row = svc.append_deployment(repo_id="repo-1", agent_id="a-1", stage="dev", image_tag="t")
    item = svc._store.get((_DEPLOYMENT_PK, row.seq_key))
    assert item is not None
    assert item["agent_id"] == "a-1"


@pytest.mark.parametrize("ids", [{}, {"agent_id": "a-1", "mcp_server_id": "mcp-1"}])
def test_append_deployment_needs_exactly_one_artifact(m, ids):
    svc = _svc(m)
    with pytest.raises(ValueError):
        svc.append_deployment(repo_id="repo-1", stage="dev", image_tag="t", **ids)


def test_find_repository_by_mcp_server_id(m):
    svc = _svc(m)
    seed(svc._store, _to_item(_REPOSITORY_PK, _mcp_repo()))
    found = svc.find_repository_by_mcp_server_id("mcp-1")
    assert found is not None
    assert found.id == "repo-1"
    assert svc.find_repository_by_mcp_server_id("mcp-x") is None


_GW: dict[str, Any] = dict(
    gateway_arn="arn:aws:bedrock-agentcore:us-east-1:111111111111:gateway/gw-1",
    gateway_id="gw-1",
    gateway_url="https://gw-1.example",
)


def test_get_repo_adopts_the_built_gateway_once(m):
    svc = _svc(m)
    seed(svc._store, _to_item(_REPOSITORY_PK, _mcp_repo(**_GW)))
    repo = svc.get_repo("repo-1")
    assert repo is not None
    assert repo.id == "repo-1"
    m.mcp_registry.get.assert_called_once_with("mcp-1")
    m.mcp_identity.adopt_built_gateway.assert_called_once_with(m.mcp_registry.get.return_value, **_GW)


def _adopted(**over):
    from models.agent import IdentityStatus

    fields: dict[str, Any] = dict(_GW, identity_status=IdentityStatus.PROVISIONED)
    fields.update(over)
    return _mcp(**fields)


def test_an_already_adopted_gateway_is_not_re_adopted(m):
    svc = _svc(m)
    seed(svc._store, _to_item(_REPOSITORY_PK, _mcp_repo(**_GW)))
    m.mcp_registry.get.return_value = _adopted()
    svc.get_repo("repo-1")
    svc.list_repositories()
    m.mcp_identity.adopt_built_gateway.assert_not_called()


@pytest.mark.parametrize(
    "drift",
    [
        {"gateway_arn": "arn:other"},
        {"gateway_id": "gw-other"},
        {"gateway_url": "https://other.example"},
        {"identity_status": "pending"},
    ],
)
def test_any_drift_or_pending_re_adopts_once(m, drift):
    from models.agent import IdentityStatus

    if "identity_status" in drift:
        drift = {"identity_status": IdentityStatus.PENDING}
    svc = _svc(m)
    seed(svc._store, _to_item(_REPOSITORY_PK, _mcp_repo(**_GW)))
    m.mcp_registry.get.return_value = _adopted(**drift)
    svc.get_repo("repo-1")
    m.mcp_identity.adopt_built_gateway.assert_called_once()


def test_list_repositories_adopts_the_built_gateway(m):
    svc = _svc(m)
    seed(svc._store, _to_item(_REPOSITORY_PK, _mcp_repo(**_GW)))
    assert [r.id for r in svc.list_repositories()] == ["repo-1"]
    m.mcp_identity.adopt_built_gateway.assert_called_once()


def test_a_read_survives_an_adopt_failure(m):
    svc = _svc(m)
    seed(svc._store, _to_item(_REPOSITORY_PK, _mcp_repo(**_GW)))
    m.mcp_identity.adopt_built_gateway.side_effect = RuntimeError("GetGateway failed")
    repo = svc.get_repo("repo-1")
    assert repo is not None
    assert repo.gateway_arn == _GW["gateway_arn"]
    assert len(svc.list_repositories()) == 1


def test_a_read_survives_an_mcp_record_read_failure(m):
    svc = _svc(m)
    seed(svc._store, _to_item(_REPOSITORY_PK, _mcp_repo(**_GW)))
    m.mcp_registry.get.side_effect = RuntimeError("registry down")
    assert svc.get_repo("repo-1") is not None
    m.mcp_identity.adopt_built_gateway.assert_not_called()


def test_no_reconcile_before_a_gateway_is_built(m):
    svc = _svc(m)
    seed(svc._store, _to_item(_REPOSITORY_PK, _mcp_repo()))
    svc.get_repo("repo-1")
    svc.list_repositories()
    m.mcp_registry.get.assert_not_called()
    m.mcp_identity.adopt_built_gateway.assert_not_called()


def test_agent_repos_are_never_reconciled(m):
    svc = _svc(m)
    seed(
        svc._store,
        _to_item(_REPOSITORY_PK, _mcp_repo(artifact_kind="agent", mcp_server_id=None, agent_id="a-1")),
    )
    svc.list_repositories()
    m.mcp_identity.adopt_built_gateway.assert_not_called()


def test_deployment_history_of_an_mcp_repo(m):
    svc = _svc(m)
    svc.append_deployment(
        repo_id="repo-1",
        mcp_server_id="mcp-1",
        stage="dev",
        image_tag="c" * 40,
        outcome=DeploymentOutcome.SUCCEEDED,
    )
    rows = svc.list_deployments("repo-1", stage="dev")
    assert [r.mcp_server_id for r in rows] == ["mcp-1"]


def test_set_repo_vars_fails_loud_when_agp_api_url_is_empty_for_an_mcp_repo(m):
    """The gateway scaffold's ``deploy.yml`` reads ``MCP_SERVER_ID``/``CONNECTION_ID``/
    ``AGP_API_URL``; the write client drops empties, so a blank ``AGP_API_URL`` used to leave the
    step ``done`` and the gateway deploy unable to notify AGP. The MCP variant of the contract
    check fails the step and names the setting and the tfvar."""
    svc = _svc(m)
    svc._repo_vars["AGP_API_URL"] = ""
    _add(svc)
    repo = _materialize(svc)
    m.rollout.set_ci_vars.assert_not_called()
    step = next(s for s in repo.steps if s.key == "set_repo_vars")
    assert step.status.value == "failed"
    assert step.error is not None
    assert "AGP_API_URL" in step.error and "agp_api_url" in step.error, step.error
