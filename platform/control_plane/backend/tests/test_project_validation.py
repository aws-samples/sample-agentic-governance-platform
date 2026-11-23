"""The framework comes from the catalog, not from a constant.

The rule is a relation: the ``framework`` in ``agent_config`` must equal the ``framework`` on the
registered template being materialized. AGP validates consistency between a request and its own catalog
and holds no opinion about which frameworks exist, because ``framework`` rides onto the governed Agent
record and a record disagreeing with its template lies about its own contents. Covered here: the
model-level rule (both arms and the unknown-framework fold), the service threading it from a real
registry record, and the HTTP statuses the two refusals surface as, 422 for a mismatch and 400 for a
malformed ``agent_name``, from the same route.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from models.project import AGENT_NAME_RE
from services.project_errors import ProjectError
from services.project_service import ProjectService
from services.template_registry import TemplateRecord, TemplateRegistry, template_id_for

FIXED = datetime(2026, 8, 6, tzinfo=timezone.utc)


def test_SUPPORTED_FRAMEWORK_is_no_longer_importable():
    """The constant's two readers were in different modules, so "the gate is gone" and "the constant is
    gone" are separable facts; leaving the name behind would let a single-framework gate grow back."""
    import models.project as project_models

    assert not hasattr(project_models, "SUPPORTED_FRAMEWORK")


def test_a_framework_mismatch_names_BOTH_values():
    """The operator cannot see the template's framework, so the message names both."""
    from models.project import validate_agent_config

    with pytest.raises(ValueError) as err:
        validate_agent_config({"framework": "strands", "agent_name": "fraud_agent"}, "langgraph")

    message = str(err.value)
    assert "strands" in message and "langgraph" in message


def test_the_mismatch_raises_its_OWN_type_that_is_still_a_ValueError():
    """The distinct type is what makes the 422 reachable; subclassing ``ValueError`` keeps every caller
    that catches the base catching it. A malformed name is a plain ``ValueError``, not this type."""
    from models.project import FrameworkMismatchError, validate_agent_config

    with pytest.raises(FrameworkMismatchError):
        validate_agent_config({"framework": "crewai", "agent_name": "a1"}, "langgraph")

    assert issubclass(FrameworkMismatchError, ValueError)

    with pytest.raises(ValueError) as err:
        validate_agent_config({"framework": "langgraph", "agent_name": "my-agent"}, "langgraph")
    assert not isinstance(err.value, FrameworkMismatchError)


def test_a_framework_mismatch_renders_422_with_its_own_sentence():
    """The one row of the class: 422, the message verbatim, the code ``project.framework_mismatch``."""
    from models.project import FrameworkMismatchError

    err = FrameworkMismatchError(
        "agent_config framework 'strands' does not match the registered template's framework 'langgraph'"
    )

    assert (err.status_code, err.kind, err.code) == (422, "framework_mismatch", "project.framework_mismatch")
    assert err.detail == str(err) == err.message
    assert err.body() == {"detail": err.message, "code": "project.framework_mismatch"}


def test_a_non_strands_framework_passes_when_the_template_agrees():
    from models.project import validate_agent_config

    validate_agent_config({"framework": "langgraph", "agent_name": "risk_agent"}, "langgraph")


def test_every_shipped_framework_validates_against_its_own_template():
    from models.project import validate_agent_config

    for framework in ("strands", "langgraph", "crewai", "harness"):
        validate_agent_config({"framework": framework, "agent_name": "a1"}, framework)


def test_an_UNKNOWN_template_framework_skips_the_relation_but_not_the_name_check():
    """An empty ``template_framework`` means AGP does not know the template's framework (a legacy row,
    or the on-disk seed path) and the relation folds; ``agent_name`` is validated against a regex AGP
    owns outright, so an unknown framework never becomes a hole in that check."""
    from models.project import validate_agent_config

    validate_agent_config({"framework": "anything", "agent_name": "a1"}, "")

    with pytest.raises(ValueError, match="invalid agent_config agent_name"):
        validate_agent_config({"framework": "anything", "agent_name": "1bad"}, "")


def test_a_MISSING_framework_field_still_fails_against_a_known_template():
    """Only the template side being unknown relaxes the rule; a request that omits the field is a
    mismatch, not a fold."""
    from models.project import validate_agent_config

    with pytest.raises(ValueError):
        validate_agent_config({"agent_name": "a1"}, "langgraph")


def test_the_agent_name_check_is_unchanged():
    from models.project import validate_agent_config

    validate_agent_config({"framework": "strands", "agent_name": "a" * 32}, "strands")

    for bad in ("a" * 33, "1agent", "my-agent", ""):
        with pytest.raises(ValueError, match="invalid agent_config agent_name"):
            validate_agent_config({"framework": "strands", "agent_name": bad}, "strands")


def _service(*, template_registry, scaffold_dir, ids=("proj-1", "repo-1", "repo-2")):
    registry = MagicMock()
    registry.create.return_value = SimpleNamespace(id="agent-1", agent_arn=None, name="p1_agent")

    conn = MagicMock()
    conn.get_connection.return_value = SimpleNamespace(org="acme", base_url=None)
    conn.get_bearer_token.return_value = "ghp_secret_token"

    it = iter(ids)
    svc = ProjectService(
        table_name="",
        agent_templates_dir=str(scaffold_dir),
        registry=registry,
        identity=MagicMock(),
        connection_service=conn,
        github_repo_service=MagicMock(),
        template_registry=template_registry,
        new_id=lambda: next(it),
        now=lambda: FIXED,
    )
    svc.create_project(name="p1", connection_id="c1", tenant_id="default", description="", created_by="op@x")
    return svc, registry


@pytest.fixture
def strands_and_langgraph_scaffold_dir(tmp_path):
    """Two real scaffolds on disk, so the service is constructible the way the rest of the suite builds it."""
    for name in ("strands-agentcore", "langgraph-agentcore"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "Dockerfile").write_bytes(b"FROM python:3.11-slim\n")
    return tmp_path


@pytest.fixture
def catalog():
    """A real ``TemplateRegistry`` on its local store with two registered templates: the value under
    test is reached through ``template_id_for(name)`` key derivation, and a mock's ``get`` would answer
    for any key, including a wrong one."""
    reg = TemplateRegistry(table_name="", now=lambda: FIXED)
    for name, framework in (("strands-agentcore", "strands"), ("langgraph-agentcore", "langgraph")):
        reg.create(
            TemplateRecord(
                id=template_id_for(name),
                name=name,
                connection_id="c1",
                created_at=FIXED.isoformat(),
                framework=framework,
                source_org="acme",
                source_repo=name,
            )
        )
    return reg


def test_add_repo_validates_against_the_REGISTERED_template(catalog, strands_and_langgraph_scaffold_dir):
    """``langgraph`` is accepted for the langgraph template and rides onto the governed Agent record."""
    svc, registry = _service(template_registry=catalog, scaffold_dir=strands_and_langgraph_scaffold_dir)

    repo = svc.add_repo(
        project_id="proj-1",
        name="risk-agent",
        template_name="langgraph-agentcore",
        agent_config={"framework": "langgraph", "agent_name": "risk_agent"},
        created_by="op@x",
        principal=SimpleNamespace(oid="O1", email="e@x"),
    )

    assert repo.cicd_status == "provisioning"
    assert registry.create.call_args.args[0].framework == "langgraph"


def test_add_repo_refuses_a_mismatch_BEFORE_the_agent_is_pre_registered(
    catalog, strands_and_langgraph_scaffold_dir
):
    """Order is the assertion: a refusal arriving after ``registry.create`` would leave an operator
    cleaning up a half-materialized agent for a request that could never succeed."""
    svc, registry = _service(template_registry=catalog, scaffold_dir=strands_and_langgraph_scaffold_dir)

    with pytest.raises(ValueError) as err:
        svc.add_repo(
            project_id="proj-1",
            name="risk-agent",
            template_name="langgraph-agentcore",
            agent_config={"framework": "strands", "agent_name": "risk_agent"},
            created_by="op@x",
            principal=SimpleNamespace(oid="O1", email="e@x"),
        )

    assert "strands" in str(err.value) and "langgraph" in str(err.value)
    registry.create.assert_not_called()


def test_add_repo_refuses_a_malformed_agent_name_as_a_project_error_kind(
    catalog, strands_and_langgraph_scaffold_dir
):
    """The model's ``ValueError`` becomes ``ProjectError(kind="invalid_agent_name")`` inside the
    service, where the template's kind is known, carrying the model's sentence; nothing is
    pre-registered."""
    svc, registry = _service(template_registry=catalog, scaffold_dir=strands_and_langgraph_scaffold_dir)

    with pytest.raises(ProjectError) as err:
        svc.add_repo(
            project_id="proj-1",
            name="risk-agent",
            template_name="langgraph-agentcore",
            agent_config={"framework": "langgraph", "agent_name": "my-agent"},
            created_by="op@x",
            principal=SimpleNamespace(oid="O1", email="e@x"),
        )

    assert err.value.kind == "invalid_agent_name"
    assert err.value.status_code == 400
    assert err.value.detail == err.value.message
    assert (
        err.value.message == f"invalid agent_config agent_name 'my-agent'; must match {AGENT_NAME_RE.pattern}"
    )
    registry.create.assert_not_called()


def test_add_repo_with_an_UNREGISTERED_template_falls_back_to_unknown(
    catalog, strands_and_langgraph_scaffold_dir
):
    """A template name with no catalog row is not refused here: the framework is unknown and the
    relation folds, which keeps the on-disk seed path working."""
    svc, _registry = _service(template_registry=catalog, scaffold_dir=strands_and_langgraph_scaffold_dir)

    repo = svc.add_repo(
        project_id="proj-1",
        name="ad-hoc",
        template_name="strands-agentcore",
        agent_config={"framework": "strands", "agent_name": "a1"},
        created_by="op@x",
        principal=SimpleNamespace(oid="O1", email="e@x"),
    )
    assert repo.template_name == "strands-agentcore"

    svc2, _ = _service(
        template_registry=catalog, scaffold_dir=strands_and_langgraph_scaffold_dir, ids=("proj-2", "repo-9")
    )
    repo2 = svc2.add_repo(
        project_id="proj-2",
        name="ad-hoc",
        template_name="never-registered",
        agent_config={"framework": "whatever", "agent_name": "a2"},
        created_by="op@x",
        principal=SimpleNamespace(oid="O1", email="e@x"),
    )
    assert repo2.template_name == "never-registered"


def test_a_template_registered_with_a_PADDED_framework_still_materializes(strands_and_langgraph_scaffold_dir):
    """The upload guard strips the framework before storing it, so a template registered as
    ``"  langgraph  "`` materializes against the unpadded value a UI sends. Driven through the real
    upload verb so the write path and the read path are shown to agree."""
    from services.github_template_service import GitHubTemplateService

    catalog = TemplateRegistry(table_name="", now=lambda: FIXED)

    gh = MagicMock()
    gh.create_repo_from_zip.return_value = "https://github.com/acme/padded-agentcore"
    connections = MagicMock()
    connections.get_connection.return_value = SimpleNamespace(org="acme", base_url=None)
    connections.get_bearer_token.return_value = "ghp_secret_token"

    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w") as zf:
        zf.writestr("Dockerfile", "FROM python:3.11-slim\n")

    GitHubTemplateService(
        github_repo_service=gh,
        connection_service=connections,
        template_registry=catalog,
        now=lambda: FIXED.isoformat(),
    ).upload_template(
        "c1",
        zip_bytes=buf.getvalue(),
        name="padded-agentcore",
        description="",
        framework="  langgraph  ",
        aws_services=[],
        tags=[],
    )

    svc, registry = _service(template_registry=catalog, scaffold_dir=strands_and_langgraph_scaffold_dir)
    repo = svc.add_repo(
        project_id="proj-1",
        name="risk-agent",
        template_name="padded-agentcore",
        agent_config={"framework": "langgraph", "agent_name": "risk_agent"},
        created_by="op@x",
        principal=SimpleNamespace(oid="O1", email="e@x"),
    )

    assert repo.cicd_status == "provisioning"
    assert registry.create.call_args.args[0].framework == "langgraph"


def test_a_broken_catalog_does_NOT_silently_accept_any_framework(strands_and_langgraph_scaffold_dir):
    """A store fault propagates; folding it into the unknown case would disable the governance gate
    exactly when the store hiccups."""

    class _BrokenRegistry:
        def get(self, connection_id, template_id):
            raise RuntimeError("ddb unavailable")

    svc, registry = _service(
        template_registry=_BrokenRegistry(), scaffold_dir=strands_and_langgraph_scaffold_dir
    )

    with pytest.raises(RuntimeError):
        svc.add_repo(
            project_id="proj-1",
            name="risk-agent",
            template_name="langgraph-agentcore",
            agent_config={"framework": "anything", "agent_name": "a1"},
            created_by="op@x",
            principal=SimpleNamespace(oid="O1", email="e@x"),
        )
    registry.create.assert_not_called()


def _route_client(monkeypatch, *, side_effect):
    """A TestClient over the real projects route with a MagicMock service that raises ``side_effect``
    from ``add_repo``, on an app carrying the one ``AppError`` handler ``main`` registers."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.setenv("AUTH_PROVIDER", "entra")
    monkeypatch.setenv("USE_DEV_AUTH", "False")
    monkeypatch.setenv("ENTRA_TENANT_ID", "00000000-0000-0000-0000-000000000001")
    monkeypatch.setenv("ENTRA_AUDIENCE", "api://agp")
    monkeypatch.setenv("ENTRA_BACKEND_CLIENT_ID", "backend-client-id")
    monkeypatch.setenv("ENTRA_SPA_CLIENT_ID", "spa-client-id")
    monkeypatch.setenv("ENTRA_ROLE_ADMIN", "Platform.Admin")
    monkeypatch.setenv("ENTRA_ROLE_OPERATOR", "Platform.Operator")
    monkeypatch.setenv("ENTRA_ROLE_VIEWER", "Platform.Viewer")

    import sys

    for mod in (
        "core.rbac",
        "core.security_entra",
        "core.config",
        "api.routes.project_repos",
        "api.routes.projects",
        "api.routes.repositories",
        "api.routes.users",
    ):
        sys.modules.pop(mod, None)

    import api.routes.project_repos as project_repos_module
    import api.routes.projects as projects_module
    import api.routes.users as users_module
    from core.errors import install_error_handlers
    from services.project_resolver import ProjectContext
    from services.tenant_resolver import TenantContext

    svc = MagicMock()
    svc.add_repo.side_effect = side_effect
    projects_module._svc = svc
    projects_module._role_svc = MagicMock()
    projects_module._role_svc.has_role_rows.return_value = False
    projects_module._role_svc.list_all_strict.return_value = []

    class _Resolver:
        def __init__(self, ctx):
            self._ctx = ctx
            self.invalidate = MagicMock()

        async def resolve(self, principal):
            return self._ctx

    users_module._tenant_resolver = _Resolver(
        TenantContext(is_global=True, tenant_ids=frozenset(), tenants=())
    )
    users_module._project_resolver = _Resolver(ProjectContext(is_global=True, roles={}))

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(projects_module.router, prefix="/api/v1")
    app.include_router(project_repos_module.router, prefix="/api/v1")
    return TestClient(app)


def _post_repo(client, agent_config):
    claims = {"oid": "op-oid", "preferred_username": "op@x.com", "roles": ["Platform.Operator"]}
    with patch("core.security_entra.verify_entra_token", return_value=claims):
        return client.post(
            "/api/v1/projects/p1/repos",
            json={
                "name": "risk-agent",
                "template_name": "langgraph-agentcore",
                "agent_config": agent_config,
            },
            headers={"Authorization": "Bearer fake-token"},
        )


def test_the_route_maps_a_framework_mismatch_to_422_with_both_values(monkeypatch):
    """422 and not 400: the body is well-formed and one field's value is unprocessable against this
    template. ``str(err)`` reaches the client deliberately, since it names the template's framework."""
    from models.project import FrameworkMismatchError

    client = _route_client(
        monkeypatch,
        side_effect=FrameworkMismatchError(
            "agent_config framework 'strands' does not match the registered template's framework 'langgraph'"
        ),
    )

    resp = _post_repo(client, {"framework": "strands", "agent_name": "risk_agent"})

    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert "strands" in detail and "langgraph" in detail
    assert resp.json()["code"] == "project.framework_mismatch"


def test_the_route_still_maps_a_bad_agent_name_to_400(monkeypatch):
    """The other half of the pair: a malformed ``agent_name`` cannot be a legal AWS name stem under any
    state of the catalog, so it is a 400 carrying the model's sentence, through the service's kind."""
    client = _route_client(
        monkeypatch,
        side_effect=ProjectError(
            "invalid agent_config agent_name 'my-agent'; must match ...", kind="invalid_agent_name"
        ),
    )

    resp = _post_repo(client, {"framework": "langgraph", "agent_name": "my-agent"})

    assert resp.status_code == 400, resp.text
    assert resp.json() == {
        "detail": "invalid agent_config agent_name 'my-agent'; must match ...",
        "code": "project.invalid_agent_name",
    }


def test_the_route_maps_an_UNREADABLE_catalog_to_503(monkeypatch):
    """``add_repo`` reads the catalog synchronously to validate the framework, which puts
    ``TemplateRegistryError`` on the request path; a store fault reaches the client as the retryable
    503 with a fixed detail."""
    from services.template_registry import TemplateRegistryError

    client = _route_client(
        monkeypatch, side_effect=TemplateRegistryError("Could not read the template catalog")
    )

    resp = _post_repo(client, {"framework": "langgraph", "agent_name": "risk_agent"})

    assert resp.status_code == 503, resp.text
    assert resp.json()["detail"] == "could not read the template catalog"


def test_the_route_maps_a_MALFORMED_catalog_key_to_422_not_503(monkeypatch):
    """``TemplateRegistryValidationError`` subclasses ``TemplateRegistryError`` and renders the permanent
    422, so a request whose key can never be built is never advertised as retryable. Reachable because
    nothing validates ``ProjectCreate.connection_id``."""
    from services.template_registry import TemplateRegistryValidationError

    client = _route_client(
        monkeypatch, side_effect=TemplateRegistryValidationError("connection_id must not be empty")
    )

    resp = _post_repo(client, {"framework": "langgraph", "agent_name": "risk_agent"})

    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == "invalid template or connection identifier"
