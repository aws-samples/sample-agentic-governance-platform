"""The Studio HTTP routes: ``POST /studio/draft`` and
``POST /projects/{id}/repos/{repo_id}/studio-proposal``.

Fixture idiom is ``test_eval_routes.py``: the real ``require_role`` / ``current_principal`` /
tenant + project ctx chain against a mocked ``verify_entra_token``, fake resolvers on the
``users`` module, ``MagicMock`` services on the ``projects`` module. The draft function and the
proposal service are faked on the ``studio`` module: their own suites cover their logic, these
tests pin the gates, the limits and the error mapping.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_projects_role_gating import (
    _claims_for,
    _detail,
    _FakeProjectResolver,
    _FakeTenantResolver,
    _FakeTenantService,
    _project,
    _project_context,
    _tenant_context,
)

pytestmark = pytest.mark.usefixtures("reset_modules")

DRAFT = "/api/v1/studio/draft"
PROPOSAL = "/api/v1/projects/proj-1/repos/r-1/studio-proposal"
MB = 1024 * 1024
AGENT_REQ = {"kind": "agent", "system_prompt": "Be helpful.", "skills": [], "declared_mcp_server_ids": []}
MCP_REQ = {
    "kind": "mcp_server",
    "openapi_text": "openapi: 3.0.3",
    "exposed_operations": ["getPolicy"],
    "auth": "api_key",
    "api_key": "k",
}
AGENT_BLOCK = {
    "kind": "agent",
    "name": "a",
    "system_prompt": "p",
    "prompt_file": None,
    "skill_files": [],
    "mcp_block_names": [],
    "eval_items": [],
}
_FAKE_TOKEN = "ghp_" + "0" * 36
CREATED = {
    "branch": "studio/r-1",
    "pr_number": 7,
    "pr_url": "https://github.com/acme/x/pull/7",
    "files": ["prompt/system.md"],
}


@pytest.fixture
def env(entra_settings):
    role_svc = MagicMock()
    role_svc.has_role_rows.return_value = True
    role_svc.list_for_project_strict.return_value = []
    project_svc = MagicMock()
    project_svc.get_project.return_value = _detail()
    project_svc.get_repo.return_value = SimpleNamespace(id="r-1", project_id="proj-1")
    proposal = MagicMock()
    proposal.propose.return_value = CREATED
    draft = MagicMock(return_value={"blocks": [], "unused_files": []})
    return SimpleNamespace(
        role_svc=role_svc, project_svc=project_svc, proposal=proposal, draft=draft, patchers=[]
    )


@pytest.fixture
def client_for(env):
    def _make(*, platform_role="operator", project_role="maintainer", signed_in=True):
        import api.routes.projects as projects_module
        import api.routes.studio as studio_module
        import api.routes.tenants as tenants_module
        import api.routes.users as users_module
        from core.errors import install_error_handlers

        users_module._tenant_resolver = _FakeTenantResolver(_tenant_context())
        users_module._project_resolver = _FakeProjectResolver(
            _project_context(is_global=platform_role == "admin", role=project_role)
        )
        tenants_module._svc = _FakeTenantService({"default"})
        projects_module._svc = env.project_svc
        projects_module._role_svc = env.role_svc
        studio_module._svc = env.proposal
        studio_module.draft_composition = env.draft

        app = FastAPI()
        install_error_handlers(app)
        app.include_router(studio_module.router, prefix="/api/v1")
        patcher = patch("core.security_entra.verify_entra_token", return_value=_claims_for(platform_role))
        patcher.start()
        env.patchers.append(patcher)
        headers = {"Authorization": "Bearer user-token"} if signed_in else {}
        return TestClient(app, headers=headers)

    yield _make
    for p in env.patchers:
        p.stop()


def _draft_body(**over):
    return {"project_id": "proj-1", "description": "Triage claims", "files": [], **over}


def test_both_handlers_are_sync(entra_settings):
    import api.routes.studio as studio_module

    assert not inspect.iscoroutinefunction(studio_module.draft)
    assert not inspect.iscoroutinefunction(studio_module.propose)


def test_draft_returns_the_service_body(client_for, env):
    env.draft.return_value = {"blocks": [AGENT_BLOCK], "unused_files": ["x.md"]}
    r = client_for().post(DRAFT, json=_draft_body(files=[{"name": "x.md", "text": "hi"}]))
    assert r.status_code == 200
    assert r.json() == {"blocks": [AGENT_BLOCK], "unused_files": ["x.md"]}
    args, kwargs = env.draft.call_args
    assert args == ("Triage claims", [("x.md", "hi")])
    assert kwargs == {"model_id": "us.anthropic.claude-sonnet-4-6"}


def test_draft_needs_a_signed_in_user(client_for, env):
    assert client_for(signed_in=False).post(DRAFT, json=_draft_body()).status_code in (401, 403)
    env.draft.assert_not_called()


def test_draft_needs_maintainer_on_the_project(client_for, env):
    r = client_for(project_role="viewer").post(DRAFT, json=_draft_body())
    assert r.status_code == 403 and r.json()["detail"] == "insufficient project role"
    env.draft.assert_not_called()


def test_draft_on_an_invisible_project_is_404(client_for, env):
    env.project_svc.get_project.return_value = None
    assert client_for().post(DRAFT, json=_draft_body()).status_code == 404
    env.draft.assert_not_called()


def test_draft_timeout_is_504(client_for, env):
    from services.studio_draft import StudioDraftTimeout

    env.draft.side_effect = StudioDraftTimeout("read timeout")
    r = client_for().post(DRAFT, json=_draft_body())
    assert r.status_code == 504
    assert r.json() == {
        "detail": "The draft took too long. Compose manually or try again.",
        "code": "studio.draft_timeout",
    }


def test_draft_model_error_is_502(client_for, env):
    from services.studio_draft import StudioDraftError

    env.draft.side_effect = StudioDraftError("AccessDenied arn:aws:bedrock:...")
    r = client_for().post(DRAFT, json=_draft_body())
    assert r.status_code == 502
    assert r.json() == {
        "detail": "The draft failed. Compose manually or try again.",
        "code": "studio.draft_failed",
    }


@pytest.mark.parametrize(
    "body",
    [
        _draft_body(description=""),
        _draft_body(description="x" * 4001),
        _draft_body(files=[{"name": f"f{i}.md", "text": "x"} for i in range(11)]),
        _draft_body(files=[{"name": "big.yaml", "text": "x" * (MB + 1)}]),
        _draft_body(files=[{"name": "big.md", "text": "é" * (MB // 2 + 1)}]),
        _draft_body(project_id=None),
    ],
)
def test_draft_size_limits_are_422(client_for, env, body):
    assert client_for().post(DRAFT, json=body).status_code == 422
    env.draft.assert_not_called()


def test_draft_at_the_limits_is_accepted(client_for, env):
    files = [{"name": f"f{i}.md", "text": "x" * MB} for i in range(10)]
    assert client_for().post(DRAFT, json=_draft_body(description="x" * 4000, files=files)).status_code == 200


def test_proposal_returns_201_with_the_service_body(client_for, env):
    r = client_for().post(PROPOSAL, json=AGENT_REQ)
    assert r.status_code == 201 and r.json() == CREATED
    project, repo, req = env.proposal.propose.call_args.args
    assert project is env.project_svc.get_project.return_value.project
    assert repo is env.project_svc.get_repo.return_value
    assert req == AGENT_REQ
    caller = env.proposal.propose.call_args.kwargs["caller"]
    assert caller.oid == "operator-oid" and caller.email == "operator@x.com"


@pytest.mark.parametrize("body", [AGENT_REQ, MCP_REQ])
def test_proposal_reaches_the_service_as_sent(client_for, env, body):
    assert client_for().post(PROPOSAL, json=body).status_code == 201
    assert env.proposal.propose.call_args.args[2] == body


def test_proposal_with_an_unknown_field_is_422_before_the_service(client_for, env):
    r = client_for().post(PROPOSAL, json={**AGENT_REQ, "shoe_size": 42})
    assert r.status_code == 422
    assert "shoe_size" in r.text
    env.proposal.propose.assert_not_called()


def test_proposal_with_an_unknown_kind_is_422_before_the_service(client_for, env):
    r = client_for().post(PROPOSAL, json={**AGENT_REQ, "kind": "zzz"})
    assert r.status_code == 422
    assert "kind" in r.text
    env.proposal.propose.assert_not_called()


def test_an_agent_proposal_without_skills_or_mcp_ids_reaches_the_service_without_them(client_for, env):
    body = {"kind": "agent", "system_prompt": "Be helpful."}
    assert client_for().post(PROPOSAL, json=body).status_code == 201
    assert env.proposal.propose.call_args.args[2] == body


def test_a_skill_with_an_unknown_field_is_422_before_the_service(client_for, env):
    skills = [{"name": "s", "content": "c", "shoe_size": 42}]
    r = client_for().post(PROPOSAL, json={**AGENT_REQ, "skills": skills})
    assert r.status_code == 422
    assert "shoe_size" in r.text
    env.proposal.propose.assert_not_called()


def test_studio_routes_declare_their_bodies(entra_settings):
    import api.routes.studio as studio_module

    app = FastAPI()
    app.include_router(studio_module.router, prefix="/api/v1")
    doc = app.openapi()
    schemas = doc["components"]["schemas"]
    draft = doc["paths"][DRAFT]["post"]
    proposal = doc["paths"]["/api/v1/projects/{id}/repos/{repo_id}/studio-proposal"]["post"]

    def ref(node: dict) -> str:
        return node["content"]["application/json"]["schema"]["$ref"].rsplit("/", 1)[1]

    assert ref(draft["responses"]["200"]) == "DraftResponse"
    assert (
        schemas["DraftResponse"]["properties"]["blocks"]["items"]["discriminator"]["propertyName"] == "kind"
    )
    assert ref(proposal["requestBody"]) == "ProposalRequest"
    assert schemas["ProposalRequest"]["discriminator"] == {
        "propertyName": "kind",
        "mapping": {
            "mcp_server": "#/components/schemas/McpProposalRequest",
            "agent": "#/components/schemas/AgentProposalRequest",
        },
    }
    assert ref(proposal["responses"]["201"]) == "ProposalResponse"
    assert set(schemas["ProposalResponse"]["properties"]) == {"branch", "pr_number", "pr_url", "files"}


def test_proposal_needs_maintainer_on_the_project(client_for, env):
    r = client_for(project_role="viewer").post(PROPOSAL, json=AGENT_REQ)
    assert r.status_code == 403
    env.proposal.propose.assert_not_called()


def test_proposal_needs_a_signed_in_user(client_for, env):
    assert client_for(signed_in=False).post(PROPOSAL, json=AGENT_REQ).status_code in (401, 403)
    env.proposal.propose.assert_not_called()


def test_proposal_on_a_repo_of_another_project_is_404(client_for, env):
    env.project_svc.get_repo.return_value = SimpleNamespace(id="r-1", project_id="proj-2")
    r = client_for().post(PROPOSAL, json=AGENT_REQ)
    assert r.status_code == 404
    assert r.json() == {"detail": "Repository not found", "code": "project.repo_not_found"}
    env.proposal.propose.assert_not_called()


def test_proposal_on_a_missing_repo_is_404(client_for, env):
    env.project_svc.get_repo.return_value = None
    assert client_for().post(PROPOSAL, json=AGENT_REQ).status_code == 404


@pytest.mark.parametrize(
    "exc_name, status, code",
    [
        ("StudioValidationError", 422, "studio.invalid"),
        ("StudioConflictError", 409, "studio.conflict"),
        ("StudioProviderError", 502, "studio.provider"),
    ],
)
def test_proposal_error_mapping(client_for, env, exc_name, status, code):
    import services.studio_proposal as sp

    env.proposal.propose.side_effect = getattr(sp, exc_name)("The repository is not ready yet")
    r = client_for().post(PROPOSAL, json=AGENT_REQ)
    assert r.status_code == status
    assert r.json() == {"detail": "The repository is not ready yet", "code": code}


def test_studio_error_detail_for_provider_carries_no_upstream_body(client_for, env):
    from services.studio_proposal import StudioProposalService

    env.project_svc.get_repo.return_value = SimpleNamespace(
        id="r-1", project_id="proj-1", status="ready", artifact_kind="agent", agent_id="a-1", name="x"
    )
    links = MagicMock()
    links.get_user_bearer_token.return_value = "ghu_user"
    links.list_for_principal.return_value = []
    repos = MagicMock()
    repos.read_repo.side_effect = RuntimeError(f"401 Bad credentials for token {_FAKE_TOKEN}")
    connections = MagicMock()
    connections.get_connection.return_value = SimpleNamespace(org="acme", base_url=None)
    registry = MagicMock()
    registry.get.return_value = SimpleNamespace(id="a-1", name="x", agent_arns={}, agent_arn=None)
    env.proposal = StudioProposalService(
        repos=repos,
        connections=connections,
        prs=MagicMock(),
        registry=registry,
        mcp_registry=MagicMock(),
        secrets=MagicMock(),
        links=links,
    )
    r = client_for().post(PROPOSAL, json=AGENT_REQ)
    assert r.status_code == 502 and r.json()["detail"] == "GitHub request failed"
    assert "ghp_" not in r.text and "Bad credentials" not in r.text


@pytest.mark.parametrize(
    "kind, status, repo_status, body, detail",
    [
        (
            "invalid",
            422,
            "ready",
            {**AGENT_REQ, "system_prompt": "  "},
            "Nothing to propose: add a system prompt or a skill",
        ),
        (
            "conflict",
            409,
            "creating",
            AGENT_REQ,
            "The repository is not ready yet; wait for it to finish creating",
        ),
    ],
)
def test_studio_error_detail_for_kind_carries_no_upstream_body(
    client_for, env, kind, status, repo_status, body, detail
):
    """``invalid`` and ``conflict`` send the message verbatim, so the message must be safe by
    construction: through the real service over collaborators whose every answer carries a
    token-shaped string and a provider sentence, the detail is the module's own sentence."""
    from services.studio_proposal import StudioProposalService

    env.project_svc.get_repo.return_value = SimpleNamespace(
        id="r-1",
        project_id="proj-1",
        status=repo_status,
        artifact_kind="agent",
        agent_id="a-1",
        name=_FAKE_TOKEN,
    )
    repos = MagicMock()
    repos.read_repo.side_effect = RuntimeError(f"401 Bad credentials for token {_FAKE_TOKEN}")
    links = MagicMock()
    links.get_user_bearer_token.return_value = "ghu_user"
    links.list_for_principal.return_value = []
    connections = MagicMock()
    connections.get_connection.return_value = SimpleNamespace(org="acme", base_url=None)
    registry = MagicMock()
    registry.get.return_value = SimpleNamespace(id="a-1", name=_FAKE_TOKEN, agent_arns={}, agent_arn=None)
    env.proposal = StudioProposalService(
        repos=repos,
        connections=connections,
        prs=MagicMock(),
        registry=registry,
        mcp_registry=MagicMock(),
        secrets=MagicMock(),
        links=links,
    )

    r = client_for().post(PROPOSAL, json=body)

    assert (r.status_code, r.json()) == (status, {"detail": detail, "code": f"studio.{kind}"})
    assert "ghp_" not in r.text and "Bad credentials" not in r.text


def test_proposal_body_over_10_mb_is_422(client_for, env):
    skills = [{"name": f"s{i}", "content": "x" * MB} for i in range(10)]
    r = client_for().post(PROPOSAL, json={**AGENT_REQ, "skills": skills})
    assert r.status_code == 422
    assert r.json() == {"detail": "The proposal is larger than 10 MB", "code": "studio.invalid"}
    env.proposal.propose.assert_not_called()


def test_a_4_mb_non_ascii_proposal_is_not_over_counted(client_for, env):
    skills = [{"name": f"s{i}", "content": "é" * (MB // 2)} for i in range(4)]
    assert client_for().post(PROPOSAL, json={**AGENT_REQ, "skills": skills}).status_code == 201


def test_proposal_body_must_be_an_object(client_for, env):
    assert client_for().post(PROPOSAL, json=["agent"]).status_code == 422
    env.proposal.propose.assert_not_called()


@pytest.mark.parametrize("path, body", [(DRAFT, _draft_body()), (PROPOSAL, AGENT_REQ)])
def test_a_platform_viewer_is_refused(client_for, env, path, body):
    assert client_for(platform_role="viewer").post(path, json=body).status_code == 403
    env.draft.assert_not_called()
    env.proposal.propose.assert_not_called()


@pytest.mark.parametrize("path, body", [(DRAFT, _draft_body()), (PROPOSAL, AGENT_REQ)])
def test_a_project_in_another_tenant_is_404(client_for, env, path, body):
    env.project_svc.get_project.return_value = _detail(_project(tenant_id="other"))
    r = client_for().post(path, json=body)
    assert r.status_code == 404 and r.json()["detail"] == "Project not found"
    env.draft.assert_not_called()
    env.proposal.propose.assert_not_called()


def test_proposal_service_is_built_from_the_shared_collaborators(entra_settings):
    import api.routes.projects as projects_module
    import api.routes.studio as studio_module

    svc = MagicMock()
    projects_module._svc = svc
    studio_module._svc = None
    with (
        patch("api.routes.agents.get_service") as agents,
        patch("api.routes.mcp_servers.get_service") as mcps,
        patch("api.routes.github_link.get_github_link_service") as links,
        patch("boto3.client") as boto,
    ):
        built = studio_module.get_proposal_service()
    assert built._repos is svc._rollout and built._conn is svc._conn
    assert built._registry is agents.return_value and built._mcp is mcps.return_value
    assert built._links is links.return_value
    assert built._sm is boto.return_value and boto.call_args.args == ("secretsmanager",)
    assert studio_module.get_proposal_service() is built
