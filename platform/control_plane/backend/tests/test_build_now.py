"""Build now: ``POST /projects/{id}/repos/{repo_id}/build`` and ``Repository.build_kind``.

A freshly materialized CONFIG repo has nothing pushed after the template commit, so nothing ever
triggers its first dev build. Build now starts one at the default branch's CURRENT HEAD through the
same ``RuntimeBuildService.start_runtime_build`` path rollback uses. Container repos are refused —
they need an image first, which only their own CI produces.

Harness idiom follows ``test_promote_repo.py``: the REAL in-memory ``ProjectService``
(``table_name=""``) with its collaborators mocked, the real RBAC chain against a mocked
``verify_entra_token``, and the runtime-build service as a mock nothing may get past.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from fastapi import FastAPI
from fastapi.testclient import TestClient
from seed import seed

from models.deployment import DeploymentOutcome
from models.project import Project
from models.project_role import ROLE_NAMES, ProjectRoleRecord
from models.repository import CicdStatus, Repository
from services.infra_template_registry import InfraTemplateRegistryError
from services.project_errors import ProjectError
from services.project_service import ProjectService
from services.project_store import _PROJECT_PK, _REPOSITORY_PK, _to_item
from services.repo_provider import RepoView
from services.runtime_build_service import RuntimeBuildError
from services.template_registry import TemplateRegistryError

pytestmark = pytest.mark.usefixtures("reset_modules")

FIXED = datetime(2026, 10, 5, tzinfo=timezone.utc)
FIXED_TS = FIXED.isoformat()
HEAD = "c" * 40
OPERATOR_OID = "operator-oid"


def _project():
    return Project(
        id="proj-1",
        name="Fraud Ops",
        connection_id="conn-1",
        tenant_id="default",
        description="a container",
        created_by="operator@x.com",
        created_at=FIXED_TS,
        updated_at=FIXED_TS,
    )


def _repo(*, id="r-1", template_name="harness-config", status="ready", cicd_status: CicdStatus = "deployed"):
    return Repository(
        id=id,
        project_id="proj-1",
        name="fraud-agent",
        repo_url="https://github.com/acme/fraud-agent",
        agent_id="a-1",
        template_name=template_name,
        cicd_status=cicd_status,
        status=status,
        created_by="operator@x.com",
        created_at=FIXED_TS,
        updated_at=FIXED_TS,
    )


_TEMPLATE_INFRA = {"harness-config": "agentcore-harness", "strands-agentcore": "agentcore-runtime"}
_INFRA_KIND = {"agentcore-harness": "config", "agentcore-runtime": "container"}


def _templates():
    """A template catalog double over ``_TEMPLATE_INFRA`` (template name to infra template id): two
    templates, one per build kind."""
    reg = MagicMock()

    def _get(connection_id, template_id):
        for name, infra_id in _TEMPLATE_INFRA.items():
            if template_id.endswith(name) or template_id == name:
                return SimpleNamespace(infra_template_id=infra_id)
        return None

    reg.get.side_effect = _get
    return reg


def _infra():
    """An infra catalog double over ``_INFRA_KIND`` (infra template id to build kind)."""
    reg = MagicMock()
    reg.get.side_effect = lambda connection_id, infra_id: (
        SimpleNamespace(build_kind=_INFRA_KIND[infra_id]) if infra_id in _INFRA_KIND else None
    )
    return reg


@pytest.fixture
def rollout():
    gh = MagicMock()
    gh.read_repo.return_value = RepoView(default_branch="main", head_sha=HEAD)
    return gh


@pytest.fixture
def svc(build_svc, rollout):
    conn = MagicMock()
    conn.get_connection.return_value = SimpleNamespace(org="acme", base_url=None)
    conn.get_bearer_token.return_value = "tok"
    s = ProjectService(
        table_name="",
        registry=MagicMock(),
        identity=MagicMock(),
        connection_service=conn,
        github_repo_service=rollout,
        runtime_build_service=build_svc,
        ecr_repository="platform-fallback-ecr",
        template_registry=_templates(),
        infra_template_registry=_infra(),
        now=lambda: FIXED,
    )
    seed(s._store, _to_item(_PROJECT_PK, _project()))
    seed(s._store, _to_item(_REPOSITORY_PK, _repo()))
    return s


def test_config_repo_builds_dev_at_head(svc, build_svc, rollout):
    """The build is in flight and persisted, the same in-flight state rollback arms for a non-prod stage."""
    repo = svc.build_repo("proj-1", "r-1", requested_by=OPERATOR_OID)

    kwargs = build_svc.start_runtime_build.call_args.kwargs
    assert kwargs["stage"] == "dev"
    assert kwargs["image_tag"] == HEAD
    assert kwargs["agent_id"] == "a-1"
    assert kwargs["connection_id"] == "conn-1"
    rollout.read_repo.assert_called_once_with("acme", "fraud-agent", token="tok", base_url=None)
    assert repo.cicd_status == "promoting"
    assert svc._get_repo("r-1").cicd_status == "promoting"
    assert repo.build_kind == "config"


def _mcp_repo():
    """An MCP repo riding the same config chain (the harness one, for the fixture)."""
    return Repository(
        id="r-1",
        project_id="proj-1",
        name="claims-api",
        repo_url="https://github.com/acme/claims-api",
        artifact_kind="mcp_server",
        mcp_server_id="mcp-1",
        template_name="harness-config",
        cicd_status="ready",
        status="ready",
        created_by="operator@x.com",
        created_at=FIXED_TS,
        updated_at=FIXED_TS,
    )


def test_mcp_repo_builds_its_gateway_by_mcp_server_id(svc, build_svc):
    """Build now is the gateway's first deploy, keyed by the MCP record; the build service is never handed
    an ``agent_id``."""
    seed(svc._store, _to_item(_REPOSITORY_PK, _mcp_repo()))
    svc.build_repo("proj-1", "r-1", requested_by=OPERATOR_OID)

    kwargs = build_svc.start_runtime_build.call_args.kwargs
    assert kwargs["mcp_server_id"] == "mcp-1"
    assert "agent_id" not in kwargs
    assert (kwargs["stage"], kwargs["image_tag"]) == ("dev", HEAD)
    rows = svc.list_deployments("r-1", stage="dev")
    assert [(r.mcp_server_id, r.agent_id) for r in rows] == [("mcp-1", None)]


def test_build_appends_a_started_dev_delivery_row(svc):
    svc.build_repo("proj-1", "r-1", requested_by=OPERATOR_OID)
    rows = svc.list_deployments("r-1", stage="dev")
    assert len(rows) == 1
    assert rows[0].outcome is DeploymentOutcome.STARTED
    assert rows[0].image_tag == HEAD
    assert rows[0].source_sha == HEAD
    assert rows[0].actor == OPERATOR_OID
    assert rows[0].build_id == "build-1"


def test_build_never_reads_as_a_prod_promotion_attempt(svc):
    """A Build now on a never-promoted repo stamps the non-prod clock only, so the production-serving
    banner and the list row's "last deployed" line stay silent."""
    repo = svc.build_repo("proj-1", "r-1", requested_by=OPERATOR_OID)
    stored = svc._get_repo("r-1")
    for r in (repo, stored):
        assert r.last_build_started_at == FIXED_TS
        assert r.last_promoted_at is None
        assert r.last_promoted_by is None
        assert r.last_promotion_build_id is None
        assert r.last_promoted_image_tag is None


def test_build_leaves_a_previous_prod_promotion_untouched(svc):
    """The list row reads "Last deployed <last_promoted_at> by <last_promoted_by>": a dev build must not
    move the date under the previous prod promoter's name."""
    prior = _repo()
    prior.last_promoted_at = "2026-09-01T00:00:00+00:00"
    prior.last_promoted_by = "prod-owner-oid"
    prior.last_promotion_build_id = "build-prod"
    seed(svc._store, _to_item(_REPOSITORY_PK, prior))
    svc.build_repo("proj-1", "r-1", requested_by=OPERATOR_OID)
    stored = svc._get_repo("r-1")
    assert stored.last_promoted_at == "2026-09-01T00:00:00+00:00"
    assert stored.last_promoted_by == "prod-owner-oid"
    assert stored.last_promotion_build_id == "build-prod"
    assert stored.last_build_started_at == FIXED_TS


def test_build_passes_the_strictly_resolved_kind_to_the_build_service(svc, build_svc):
    """The build service must not re-fold a catalog hiccup into a container build."""
    svc.build_repo("proj-1", "r-1", requested_by=OPERATOR_OID)
    assert build_svc.start_runtime_build.call_args.kwargs["expected_build_kind"] == "config"


def test_build_kind_mismatch_attempts_nothing_and_leaves_the_record(svc, build_svc):
    build_svc.start_runtime_build.side_effect = RuntimeBuildError("mismatch", kind="build_kind_mismatch")
    with pytest.raises(ProjectError) as err:
        svc.build_repo("proj-1", "r-1", requested_by=OPERATOR_OID)
    assert err.value.kind == "build_failed"
    assert svc._get_repo("r-1").cicd_status == "deployed"
    assert svc.list_deployments("r-1", stage="dev") == []


def test_container_repo_is_refused(svc, build_svc):
    seed(svc._store, _to_item(_REPOSITORY_PK, _repo(template_name="strands-agentcore")))
    with pytest.raises(ProjectError) as err:
        svc.build_repo("proj-1", "r-1", requested_by=OPERATOR_OID)
    assert err.value.kind == "container_build"
    build_svc.start_runtime_build.assert_not_called()


def test_repo_not_materialized_is_refused(svc, build_svc, rollout):
    """``not_ready`` is a clearer 409 than ``no_commit`` for a repo still materializing."""
    seed(svc._store, _to_item(_REPOSITORY_PK, _repo(status="provisioning")))
    with pytest.raises(ProjectError) as err:
        svc.build_repo("proj-1", "r-1", requested_by=OPERATOR_OID)
    assert err.value.kind == "not_ready"
    build_svc.start_runtime_build.assert_not_called()


def test_failed_materialize_is_refused_without_claiming_still_being_created(svc, build_svc, rollout):
    """A failed materialize is not "still being created"."""
    seed(svc._store, _to_item(_REPOSITORY_PK, _repo(status="failed")))
    with pytest.raises(ProjectError) as err:
        svc.build_repo("proj-1", "r-1", requested_by=OPERATOR_OID)
    assert err.value.kind == "materialize_failed"
    assert "still being created" not in str(err.value)
    build_svc.start_runtime_build.assert_not_called()


@pytest.mark.parametrize("view", [None, RepoView(default_branch="main", head_sha="")])
def test_repo_without_head_is_refused(svc, build_svc, rollout, view):
    rollout.read_repo.return_value = view
    with pytest.raises(ProjectError) as err:
        svc.build_repo("proj-1", "r-1", requested_by=OPERATOR_OID)
    assert err.value.kind == "no_commit"
    build_svc.start_runtime_build.assert_not_called()


def test_codebuild_in_flight_is_a_conflict_and_leaves_the_record(svc, build_svc):
    build_svc.start_runtime_build.side_effect = RuntimeBuildError("running", kind="conflict")
    with pytest.raises(ProjectError) as err:
        svc.build_repo("proj-1", "r-1", requested_by=OPERATOR_OID)
    assert err.value.kind == "conflict"
    assert svc._get_repo("r-1").cicd_status == "deployed"
    assert svc.list_deployments("r-1", stage="dev") == []


def test_own_delivery_in_flight_is_refused_before_the_build(svc, build_svc):
    svc.build_repo("proj-1", "r-1", requested_by=OPERATOR_OID)
    build_svc.start_runtime_build.reset_mock()
    with pytest.raises(ProjectError) as err:
        svc.build_repo("proj-1", "r-1", requested_by=OPERATOR_OID)
    assert err.value.kind == "promote_in_flight"
    build_svc.start_runtime_build.assert_not_called()


def test_build_start_failure_records_a_failed_attempt(svc, build_svc):
    build_svc.start_runtime_build.side_effect = RuntimeBuildError("boom", kind="start_failed")
    with pytest.raises(ProjectError) as err:
        svc.build_repo("proj-1", "r-1", requested_by=OPERATOR_OID)
    assert err.value.kind == "build_failed"
    assert svc._get_repo("r-1").cicd_status == "failed"
    rows = svc.list_deployments("r-1", stage="dev")
    assert [r.outcome for r in rows] == [DeploymentOutcome.FAILED]


@pytest.mark.parametrize(
    "registry, error",
    [("_templates", TemplateRegistryError("x")), ("_infra", InfraTemplateRegistryError("x"))],
)
def test_a_real_catalog_fault_is_build_failed(svc, build_svc, registry, error):
    """The real registries wrap ``ClientError`` in their own types; those must map, not 500."""
    getattr(svc, registry).get.side_effect = error
    with pytest.raises(ProjectError) as err:
        svc.build_repo("proj-1", "r-1", requested_by=OPERATOR_OID)
    assert err.value.kind == "build_failed"
    build_svc.start_runtime_build.assert_not_called()


def test_in_flight_is_refused_before_the_provider_read(svc, rollout):
    svc.build_repo("proj-1", "r-1", requested_by=OPERATOR_OID)
    rollout.read_repo.reset_mock()
    with pytest.raises(ProjectError):
        svc.build_repo("proj-1", "r-1", requested_by=OPERATOR_OID)
    rollout.read_repo.assert_not_called()


def test_rollback_response_carries_build_kind(svc, build_svc, monkeypatch):
    monkeypatch.setattr(svc, "_has_succeeded", lambda *a: True)
    repo = svc.rollback_repo("proj-1", "r-1", image_tag=HEAD, rolled_back_by=OPERATOR_OID, stage="dev")
    assert repo.build_kind == "config"


def test_unknown_repo_is_not_found(svc):
    with pytest.raises(ProjectError) as err:
        svc.build_repo("proj-OTHER", "r-1", requested_by=OPERATOR_OID)
    assert err.value.kind == "repo_not_found"


def test_build_kind_on_reads(svc):
    seed(svc._store, _to_item(_REPOSITORY_PK, _repo(id="r-2", template_name="strands-agentcore")))
    assert svc.get_repo("r-1").build_kind == "config"
    assert svc.get_repo("r-2").build_kind == "container"
    kinds = {r.id: r.build_kind for r in svc.get_project("proj-1").repositories}
    assert kinds == {"r-1": "config", "r-2": "container"}
    kinds = {r.id: r.build_kind for r in svc.list_repositories()}
    assert kinds == {"r-1": "config", "r-2": "container"}


def test_build_kind_is_none_when_the_chain_cannot_be_read(svc):
    svc._infra.get.side_effect = ClientError({"Error": {"Code": "Throttling"}}, "GetItem")
    assert svc.get_repo("r-1").build_kind is None
    assert [r.build_kind for r in svc.list_repositories()] == [None]


def test_build_kind_is_resolved_once_per_template_on_a_list(svc):
    seed(svc._store, _to_item(_REPOSITORY_PK, _repo(id="r-2")))
    svc._infra.get.reset_mock()
    svc.get_project("proj-1")
    assert svc._infra.get.call_count == 1


def test_a_failed_resolution_is_not_repeated_on_a_list(svc):
    seed(svc._store, _to_item(_REPOSITORY_PK, _repo(id="r-2")))
    svc._infra.get.side_effect = ClientError({"Error": {"Code": "Throttling"}}, "GetItem")
    svc._infra.get.reset_mock()
    assert [r.build_kind for r in svc.get_project("proj-1").repositories] == [None, None]
    assert svc._infra.get.call_count == 1


def test_build_kind_is_never_persisted(svc):
    repo = svc.get_repo("r-1")
    svc._save_repo(repo)
    assert svc._get_repo("r-1").build_kind is None


def _tenant_context():
    from services.tenant_resolver import TenantContext

    return TenantContext(is_global=False, tenant_ids=frozenset(["default"]), tenants=())


def _project_context(role):
    from services.project_resolver import ProjectContext

    return ProjectContext(is_global=False, roles={"proj-1": ROLE_NAMES[role]} if role else {})


class _FakeTenantResolver:
    async def resolve(self, principal):
        return _tenant_context()


class _FakeProjectResolver:
    def __init__(self, ctx):
        self._ctx = ctx
        self.invalidate = MagicMock()

    async def resolve(self, principal):
        return self._ctx

    def refresh_project(self, principal, project_id):
        return self._ctx


@pytest.fixture
def governed_role_svc():
    """A governed project (role rows exist), so the strict gate runs."""
    import api.routes.projects as projects_module

    rs = MagicMock()
    rs.has_role_rows.return_value = True
    rs.list_for_project.return_value = [
        ProjectRoleRecord(
            project_id="proj-1",
            principal_id="someone",
            principal_type="user",
            principal_display="Alex",
            role="owner",
            granted_by="seed",
            granted_at=FIXED_TS,
        )
    ]
    projects_module._role_svc = rs
    return rs


@pytest.fixture
def operator_client_factory(entra_settings, governed_role_svc, svc):
    built = []

    def _make(project_role):
        import api.routes.project_repos as project_repos_module
        import api.routes.projects as projects_module
        import api.routes.users as users_module
        from core.errors import install_error_handlers

        projects_module._svc = svc
        users_module._tenant_resolver = _FakeTenantResolver()
        users_module._project_resolver = _FakeProjectResolver(_project_context(project_role))
        app = FastAPI()
        install_error_handlers(app)
        app.include_router(projects_module.router, prefix="/api/v1")
        app.include_router(project_repos_module.router, prefix="/api/v1")
        client = TestClient(app, headers={"Authorization": "Bearer fake-token"})
        patcher = patch(
            "core.security_entra.verify_entra_token",
            return_value={
                "oid": OPERATOR_OID,
                "preferred_username": "operator@x.com",
                "roles": ["Platform.Operator"],
            },
        )
        patcher.start()
        built.append(patcher)
        return client

    yield _make
    for p in built:
        p.stop()


BUILD = "/api/v1/projects/proj-1/repos/r-1/build"


def test_route_maintainer_builds_a_config_repo(operator_client_factory, build_svc):
    r = operator_client_factory("maintainer").post(BUILD)
    assert r.status_code == 202
    assert r.json()["build_kind"] == "config"
    assert r.json()["cicd_status"] == "promoting"
    kwargs = build_svc.start_runtime_build.call_args.kwargs
    assert (kwargs["stage"], kwargs["image_tag"]) == ("dev", HEAD)


def test_route_viewer_is_refused(operator_client_factory, build_svc):
    r = operator_client_factory("viewer").post(BUILD)
    assert r.status_code == 403
    build_svc.start_runtime_build.assert_not_called()


def test_route_container_repo_is_422(operator_client_factory, svc, build_svc):
    seed(svc._store, _to_item(_REPOSITORY_PK, _repo(template_name="strands-agentcore")))
    r = operator_client_factory("maintainer").post(BUILD)
    assert r.status_code == 422
    assert r.json()["detail"] == "container repos build from CI on push"
    build_svc.start_runtime_build.assert_not_called()


def test_route_in_flight_is_409(operator_client_factory, build_svc):
    build_svc.start_runtime_build.side_effect = RuntimeBuildError("running", kind="conflict")
    r = operator_client_factory("maintainer").post(BUILD)
    assert r.status_code == 409
    assert r.json()["detail"] == ("a build is already running for this agent/stage, try again shortly")


def test_route_no_head_is_409(operator_client_factory, rollout, build_svc):
    rollout.read_repo.return_value = RepoView(default_branch="main", head_sha="")
    r = operator_client_factory("maintainer").post(BUILD)
    assert r.status_code == 409
    assert r.json()["detail"] == "repo has no commit to build yet"
    build_svc.start_runtime_build.assert_not_called()


def test_route_not_materialized_is_a_clear_409(operator_client_factory, svc, build_svc):
    seed(svc._store, _to_item(_REPOSITORY_PK, _repo(status="provisioning")))
    r = operator_client_factory("maintainer").post(BUILD)
    assert r.status_code == 409
    assert r.json()["detail"] == ("this repo is still being created — try again when materialize finishes")
    build_svc.start_runtime_build.assert_not_called()


def test_route_unknown_repo_is_404(operator_client_factory):
    r = operator_client_factory("maintainer").post("/api/v1/projects/proj-1/repos/r-NOPE/build")
    assert r.status_code == 404


def test_route_start_failure_is_502(operator_client_factory, build_svc):
    build_svc.start_runtime_build.side_effect = RuntimeBuildError("boom", kind="start_failed")
    r = operator_client_factory("maintainer").post(BUILD)
    assert r.status_code == 502
    assert r.json()["detail"] == "failed to start the build"


def test_route_catalog_fault_is_502(operator_client_factory, svc, build_svc):
    svc._infra.get.side_effect = InfraTemplateRegistryError("x")
    r = operator_client_factory("maintainer").post(BUILD)
    assert r.status_code == 502
    assert r.json()["detail"] == "failed to start the build"
    build_svc.start_runtime_build.assert_not_called()


def test_route_own_promotion_in_flight_is_409(operator_client_factory, build_svc):
    client = operator_client_factory("maintainer")
    assert client.post(BUILD).status_code == 202
    r = client.post(BUILD)
    assert r.status_code == 409
    assert r.json()["detail"] == "a promotion is already in flight"


def test_route_status_carries_build_kind(operator_client_factory, svc):
    seed(svc._store, _to_item(_REPOSITORY_PK, _repo(id="r-2", template_name="strands-agentcore")))
    client = operator_client_factory("viewer")
    assert client.get("/api/v1/projects/proj-1/repos/r-1/status").json()["build_kind"] == "config"
    assert client.get("/api/v1/projects/proj-1/repos/r-2/status").json()["build_kind"] == "container"


def test_route_status_build_kind_is_null_on_resolve_failure(operator_client_factory, svc):
    svc._infra.get.side_effect = ClientError({"Error": {"Code": "Throttling"}}, "GetItem")
    r = operator_client_factory("viewer").get("/api/v1/projects/proj-1/repos/r-1/status")
    assert r.status_code == 200
    assert r.json()["build_kind"] is None


def test_route_failed_materialize_is_its_own_409(operator_client_factory, svc, build_svc):
    seed(svc._store, _to_item(_REPOSITORY_PK, _repo(status="failed")))
    r = operator_client_factory("maintainer").post(BUILD)
    assert r.status_code == 409
    assert r.json()["detail"] == "this repo's setup failed — retry the setup before building"
    build_svc.start_runtime_build.assert_not_called()
