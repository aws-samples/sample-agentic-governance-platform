"""Build-trigger route tests.

A FastAPI dependency override replaces ``verify_github_oidc`` (no live GitHub JWKS) and the
module-level ``_conn_svc`` / ``_build_svc`` / ``_project_svc`` singletons are patched with fakes
(no live AWS). The ``AppError`` handler is mounted so the service tables render as they do on the
real app.
"""

from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.security_github_oidc import GitHubActionsClaims
from models.connection import Connection, ConnectionAuthType, ConnStatus, Provider
from services.connection_service import ConnectionError
from services.runtime_build_service import RuntimeBuildError

pytestmark = pytest.mark.usefixtures("reset_modules")


def _conn(org="acme-org"):
    return Connection(
        id="conn-1",
        provider=Provider.GITHUB,
        org=org,
        base_url=None,
        auth_type=ConnectionAuthType.PAT,
        status=ConnStatus.CONNECTED,
        secret_arn="arn:aws:secretsmanager:us-east-1:111:secret:agp/git/conn-1",
        has_secret=True,
        created_by="a@b.com",
        created_at="t",
        updated_at="t",
    )


def _proj_svc(*, repo_name="some-agent", project_conn_id="conn-1", found=True):
    """Fake ProjectService for the agent_id↔token.repository binding. Returns a repo
    (name + project_id) for find_repository_by_agent_id, and a project (connection_id) for
    get_project. `found=False` simulates an unknown agent_id (no owning repo)."""
    svc = MagicMock()
    repo = MagicMock()
    repo.name = repo_name
    repo.project_id = "proj-1"
    svc.find_repository_by_agent_id.return_value = repo if found else None
    detail = MagicMock()
    detail.project.connection_id = project_conn_id
    svc.get_project.return_value = detail
    return svc


def _build_client(*, conn_svc, build_svc, claims, proj_svc=None):
    """The dependency override is keyed off the SAME function object the route's Depends resolved:
    the builds module re-imports it fresh after module cache resets, so a top-level import here
    would be a different object and the override would silently miss."""
    import api.routes.builds as builds_module
    from core.errors import install_error_handlers

    builds_module._conn_svc = conn_svc
    builds_module._build_svc = build_svc
    builds_module._project_svc = proj_svc if proj_svc is not None else _proj_svc()

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(builds_module.router, prefix="/api/v1")
    app.dependency_overrides[builds_module.verify_github_oidc] = lambda: claims
    return TestClient(app)


def _claims(
    owner="acme-org",
    repo="some-agent",
    ref="refs/heads/main",
    sha="abc",
    actor="merging-human",
    event_name="push",
):
    return GitHubActionsClaims(
        repository=f"{owner}/{repo}",
        repository_owner=owner,
        sub="repo:x",
        ref=ref,
        sha=sha,
        workflow="build",
        actor=actor,
        event_name=event_name,
    )


_BODY: dict[str, Any] = {
    "agent_id": "agent-42",
    "image_tag": "agent-42-abc",
    "ecr_repo": "123.dkr.ecr.us-east-1.amazonaws.com/agp",
    "connection_id": "conn-1",
    "stage": "dev",
}


def test_happy_path_starts_build():
    conn_svc = MagicMock()
    conn_svc.get_connection.return_value = _conn()
    build_svc = MagicMock()
    build_svc.start_runtime_build.return_value = "rt-build:1"

    client = _build_client(conn_svc=conn_svc, build_svc=build_svc, claims=_claims())
    resp = client.post("/api/v1/builds/runtime", json=_BODY)

    assert resp.status_code == 202
    assert resp.json() == {"build_id": "rt-build:1", "status": "started"}
    build_svc.start_runtime_build.assert_called_once_with(
        agent_id="agent-42",
        image_tag="agent-42-abc",
        ecr_repo="123.dkr.ecr.us-east-1.amazonaws.com/agp",
        connection_id="conn-1",
        stage="dev",
        image_digest="",
    )


def test_org_mismatch_returns_403():
    conn_svc = MagicMock()
    conn_svc.get_connection.return_value = _conn(org="acme-org")
    build_svc = MagicMock()

    client = _build_client(conn_svc=conn_svc, build_svc=build_svc, claims=_claims(owner="evil-org"))
    resp = client.post("/api/v1/builds/runtime", json=_BODY)

    assert resp.status_code == 403
    assert resp.json() == {
        "detail": "Token repository_owner does not match connection org",
        "code": "runtime_build.token_org_mismatch",
    }
    build_svc.start_runtime_build.assert_not_called()


def test_unknown_connection_returns_404():
    conn_svc = MagicMock()
    conn_svc.get_connection.side_effect = ConnectionError("nope", kind="not_found")
    build_svc = MagicMock()

    client = _build_client(conn_svc=conn_svc, build_svc=build_svc, claims=_claims())
    resp = client.post("/api/v1/builds/runtime", json=_BODY)

    assert resp.status_code == 404
    assert resp.json() == {"detail": "Connection not found", "code": "connection.not_found"}
    assert "nope" not in resp.text
    build_svc.start_runtime_build.assert_not_called()


def test_start_build_failure_returns_502():
    conn_svc = MagicMock()
    conn_svc.get_connection.return_value = _conn()
    build_svc = MagicMock()
    build_svc.start_runtime_build.side_effect = RuntimeBuildError("start failed", kind="start_failed")
    proj_svc = _proj_svc()

    client = _build_client(conn_svc=conn_svc, build_svc=build_svc, claims=_claims(), proj_svc=proj_svc)
    resp = client.post("/api/v1/builds/runtime", json=_BODY)

    assert resp.status_code == 502
    assert resp.json() == {"detail": {"status": "failed_to_start"}, "code": "runtime_build.start_failed"}
    assert "start failed" not in resp.text
    kw = proj_svc.append_deployment.call_args.kwargs
    assert (kw["build_id"], kw["outcome"].value, kw["error"]) == (
        None,
        "failed",
        "failed to start the runtime build",
    )
    proj_svc.record_prod_candidate.assert_not_called()


@pytest.mark.parametrize(
    "kind", ["not_found", "unsupported", "build_kind_mismatch", "credential_staging_failed"]
)
def test_every_other_refusal_renders_the_failed_to_start_body_and_records_the_attempt(kind):
    conn_svc = MagicMock()
    conn_svc.get_connection.return_value = _conn()
    build_svc = MagicMock()
    build_svc.start_runtime_build.side_effect = RuntimeBuildError("store internals", kind=kind)
    proj_svc = _proj_svc()

    client = _build_client(conn_svc=conn_svc, build_svc=build_svc, claims=_claims(), proj_svc=proj_svc)
    resp = client.post("/api/v1/builds/runtime", json=_BODY)

    assert resp.status_code == 502
    assert resp.json() == {"detail": {"status": "failed_to_start"}, "code": f"runtime_build.{kind}"}
    assert proj_svc.append_deployment.call_args.kwargs["outcome"].value == "failed"


def test_start_build_in_flight_conflict_returns_409_not_502():
    """A build already running for this agent+stage maps to 409, not the generic 502, so the
    GitHub trigger job's retry loop backs off and retries instead of failing the workflow outright;
    the body carries the service's message under ``detail.message``, the shape the frontend reads,
    and no delivery row is written for a push that is simply waiting its turn."""
    conn_svc = MagicMock()
    conn_svc.get_connection.return_value = _conn()
    build_svc = MagicMock()
    build_svc.start_runtime_build.side_effect = RuntimeBuildError(
        "a build for agent_id=agent-42 stage=dev is already in progress",
        kind="conflict",
    )
    proj_svc = _proj_svc()

    client = _build_client(conn_svc=conn_svc, build_svc=build_svc, claims=_claims(), proj_svc=proj_svc)
    resp = client.post("/api/v1/builds/runtime", json=_BODY)

    assert resp.status_code == 409
    assert resp.json() == {
        "detail": {
            "status": "build_in_progress",
            "message": "a build for agent_id=agent-42 stage=dev is already in progress",
        },
        "code": "runtime_build.conflict",
    }
    proj_svc.append_deployment.assert_not_called()


def test_builds_runtime_rejects_bad_stage():
    """A stage outside ("dev","prod") → 422; the build service is never called."""
    conn_svc = MagicMock()
    conn_svc.get_connection.return_value = _conn()
    build_svc = MagicMock()

    client = _build_client(conn_svc=conn_svc, build_svc=build_svc, claims=_claims())
    resp = client.post("/api/v1/builds/runtime", json={**_BODY, "stage": "staging"})

    assert resp.status_code == 422
    assert resp.json() == {"detail": "Invalid stage", "code": "runtime_build.invalid_stage"}
    build_svc.start_runtime_build.assert_not_called()


def test_builds_runtime_rejects_agent_not_owned_by_token_repo():
    """token.repository="acme-org/repo-A" but the POSTed agent_id belongs to
    "acme-org/repo-B" → 403 (a token cannot build another repo's agent within its own org)."""
    conn_svc = MagicMock()
    conn_svc.get_connection.return_value = _conn(org="acme-org")
    build_svc = MagicMock()
    proj_svc = _proj_svc(repo_name="repo-B")

    client = _build_client(
        conn_svc=conn_svc,
        build_svc=build_svc,
        claims=_claims(owner="acme-org", repo="repo-A"),
        proj_svc=proj_svc,
    )
    resp = client.post("/api/v1/builds/runtime", json=_BODY)

    assert resp.status_code == 403
    assert resp.json() == {
        "detail": "Token repository does not own this agent",
        "code": "runtime_build.token_not_agent_owner",
    }
    build_svc.start_runtime_build.assert_not_called()


def test_builds_runtime_allows_matching_repo():
    """token.repository="acme-org/my-agent" and the agent_id's owning repo is
    "my-agent" in org "acme-org" → the binding passes and the build proceeds (202)."""
    conn_svc = MagicMock()
    conn_svc.get_connection.return_value = _conn(org="acme-org")
    build_svc = MagicMock()
    build_svc.start_runtime_build.return_value = "rt-build:3"
    proj_svc = _proj_svc(repo_name="my-agent")

    client = _build_client(
        conn_svc=conn_svc,
        build_svc=build_svc,
        claims=_claims(owner="acme-org", repo="my-agent"),
        proj_svc=proj_svc,
    )
    resp = client.post("/api/v1/builds/runtime", json=_BODY)

    assert resp.status_code == 202
    build_svc.start_runtime_build.assert_called_once()


def test_builds_runtime_allows_case_mismatched_repo():
    """GitHub org/repo logins are case-insensitive. The OIDC token carries canonical
    case ("Acme/My-Agent") while our store may hold a divergent case ("acme/my-agent").
    Both the org-match and the binding must case-fold so a legitimate build is not
    spuriously 403'd (availability; never a false-accept — both operands are trusted)."""
    conn_svc = MagicMock()
    conn_svc.get_connection.return_value = _conn(org="acme")
    build_svc = MagicMock()
    build_svc.start_runtime_build.return_value = "rt-build:4"
    proj_svc = _proj_svc(repo_name="my-agent")

    client = _build_client(
        conn_svc=conn_svc,
        build_svc=build_svc,
        claims=_claims(owner="Acme", repo="My-Agent"),
        proj_svc=proj_svc,
    )
    resp = client.post("/api/v1/builds/runtime", json=_BODY)

    assert resp.status_code == 202
    build_svc.start_runtime_build.assert_called_once()


def test_builds_runtime_unknown_agent_returns_403():
    """Fail-closed: the POSTed agent_id has no owning repo in our store
    (find_repository_by_agent_id → None) → 403, and the build service is never called."""
    conn_svc = MagicMock()
    conn_svc.get_connection.return_value = _conn(org="acme-org")
    build_svc = MagicMock()
    proj_svc = _proj_svc(found=False)

    client = _build_client(
        conn_svc=conn_svc,
        build_svc=build_svc,
        claims=_claims(),
        proj_svc=proj_svc,
    )
    resp = client.post("/api/v1/builds/runtime", json=_BODY)

    assert resp.status_code == 403
    build_svc.start_runtime_build.assert_not_called()


def test_builds_runtime_missing_project_returns_403():
    """Fail-closed: the owning repo exists but its project has vanished
    (get_project → None) → repo_org stays None → 403, service not called."""
    conn_svc = MagicMock()
    conn_svc.get_connection.return_value = _conn(org="acme-org")
    build_svc = MagicMock()
    proj_svc = _proj_svc(repo_name="some-agent")
    proj_svc.get_project.return_value = None

    client = _build_client(
        conn_svc=conn_svc,
        build_svc=build_svc,
        claims=_claims(),
        proj_svc=proj_svc,
    )
    resp = client.post("/api/v1/builds/runtime", json=_BODY)

    assert resp.status_code == 403
    build_svc.start_runtime_build.assert_not_called()


def test_builds_runtime_repo_connection_error_returns_403():
    """Fail-closed: resolving the repo's own project connection raises ConnectionError
    (e.g. connection removed) → repo_org stays None → 403 (not 500), service not called."""
    conn_svc = MagicMock()
    conn_svc.get_connection.side_effect = [
        _conn(org="acme-org"),
        ConnectionError("gone", kind="not_found"),
    ]
    build_svc = MagicMock()
    proj_svc = _proj_svc(repo_name="some-agent")

    client = _build_client(
        conn_svc=conn_svc,
        build_svc=build_svc,
        claims=_claims(),
        proj_svc=proj_svc,
    )
    resp = client.post("/api/v1/builds/runtime", json=_BODY)

    assert resp.status_code == 403
    build_svc.start_runtime_build.assert_not_called()


def test_oidc_prod_stage_is_refused():
    """The OIDC path has NO human principal, so an OWNER check is impossible here.
    Refusing `stage="prod"` is what makes the promote route's OWNER gate real rather than
    advisory — otherwise anyone who can push a workflow bypasses it entirely."""
    conn_svc = MagicMock()
    conn_svc.get_connection.return_value = _conn()
    build_svc = MagicMock()

    client = _build_client(conn_svc=conn_svc, build_svc=build_svc, claims=_claims())
    resp = client.post("/api/v1/builds/runtime", json={**_BODY, "stage": "prod"})

    assert resp.status_code == 403
    assert resp.json() == {
        "detail": "prod deploys must be initiated from AGP",
        "code": "runtime_build.prod_from_agp_only",
    }
    build_svc.start_runtime_build.assert_not_called()


def test_oidc_dev_stage_still_works():
    """Dev behaviour is UNTOUCHED by the prod refusal."""
    conn_svc = MagicMock()
    conn_svc.get_connection.return_value = _conn()
    build_svc = MagicMock()
    build_svc.start_runtime_build.return_value = "rt-build:9"

    client = _build_client(conn_svc=conn_svc, build_svc=build_svc, claims=_claims())
    resp = client.post("/api/v1/builds/runtime", json={**_BODY, "stage": "dev"})

    assert resp.status_code == 202
    build_svc.start_runtime_build.assert_called_once()


def test_prod_refusal_happens_after_binding_checks():
    """Ordering: a foreign-repo token must still get the BINDING 403, not the stage
    message. The refusal sits AFTER the binding checks so an attacker probing with a foreign
    token learns nothing about stage handling from the ordering."""
    conn_svc = MagicMock()
    conn_svc.get_connection.return_value = _conn(org="acme-org")
    proj_svc = _proj_svc(repo_name="repo-B")
    build_svc = MagicMock()

    client = _build_client(
        conn_svc=conn_svc,
        build_svc=build_svc,
        claims=_claims(owner="acme-org", repo="repo-A"),
        proj_svc=proj_svc,
    )
    resp = client.post("/api/v1/builds/runtime", json={**_BODY, "stage": "prod"})

    assert resp.status_code == 403
    assert resp.json()["detail"] != "prod deploys must be initiated from AGP"
    assert resp.json()["detail"] == "Token repository does not own this agent"
    build_svc.start_runtime_build.assert_not_called()


_CANDIDATE_BODY = {
    "agent_id": "agent-42",
    "image_tag": "agent-42-1a2b3c4",
    "ecr_repo": "some.dkr.ecr.us-east-1.amazonaws.com/agp",
    "connection_id": "conn-1",
}


def test_prod_candidate_happy_path_records_oidc_proven_actor_and_sha():
    """A merge to `main` registers the candidate — 202 + the tag from the BODY but
    the actor and sha from the VALIDATED TOKEN (never body-asserted)."""
    conn_svc = MagicMock()
    conn_svc.get_connection.return_value = _conn()
    proj_svc = _proj_svc(repo_name="some-agent")

    client = _build_client(
        conn_svc=conn_svc,
        build_svc=MagicMock(),
        claims=_claims(actor="merging-human", sha="deadbeefcafe"),
        proj_svc=proj_svc,
    )
    resp = client.post("/api/v1/builds/prod-candidate", json=_CANDIDATE_BODY)

    assert resp.status_code == 202
    assert resp.json() == {"status": "registered"}
    proj_svc.record_prod_candidate.assert_called_once_with(
        "agent-42",
        image_tag="agent-42-1a2b3c4",
        sha="deadbeefcafe",
        actor="merging-human",
    )


def test_prod_candidate_rejects_non_main_ref():
    """`main` IS the prod candidate — a dev-branch token gets 403 and nothing
    is recorded."""
    conn_svc = MagicMock()
    conn_svc.get_connection.return_value = _conn()
    proj_svc = _proj_svc(repo_name="some-agent")

    client = _build_client(
        conn_svc=conn_svc,
        build_svc=MagicMock(),
        claims=_claims(ref="refs/heads/dev"),
        proj_svc=proj_svc,
    )
    resp = client.post("/api/v1/builds/prod-candidate", json=_CANDIDATE_BODY)

    assert resp.status_code == 403
    assert resp.json() == {
        "detail": "prod candidates come from main only",
        "code": "runtime_build.prod_candidate_not_main",
    }
    proj_svc.record_prod_candidate.assert_not_called()


def test_prod_candidate_branch_check_happens_after_binding_checks():
    """Ordering proof: a foreign-repo token on a NON-`main` ref must still get the BINDING
    403, not the branch message — the ordering leaks nothing about branch handling.

    BOTH checks must be armed for this to prove anything: the ref is `dev` (so the branch
    check WOULD fire) and the repo is foreign (so the binding WOULD fire). Whichever detail
    comes back names the check that ran first. Mirrors the /runtime sibling
    `test_prod_refusal_happens_after_binding_checks`, including its negative assertion."""
    conn_svc = MagicMock()
    conn_svc.get_connection.return_value = _conn(org="acme-org")
    proj_svc = _proj_svc(repo_name="repo-B")

    client = _build_client(
        conn_svc=conn_svc,
        build_svc=MagicMock(),
        claims=_claims(owner="acme-org", repo="repo-A", ref="refs/heads/dev"),
        proj_svc=proj_svc,
    )
    resp = client.post("/api/v1/builds/prod-candidate", json=_CANDIDATE_BODY)

    assert resp.status_code == 403
    assert resp.json()["detail"] != "prod candidates come from main only"
    assert resp.json()["detail"] == "Token repository does not own this agent"
    proj_svc.record_prod_candidate.assert_not_called()


def test_prod_candidate_unknown_connection_returns_404():
    conn_svc = MagicMock()
    conn_svc.get_connection.side_effect = ConnectionError("nope", kind="not_found")
    proj_svc = _proj_svc()

    client = _build_client(
        conn_svc=conn_svc,
        build_svc=MagicMock(),
        claims=_claims(),
        proj_svc=proj_svc,
    )
    resp = client.post("/api/v1/builds/prod-candidate", json=_CANDIDATE_BODY)

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Connection not found"
    proj_svc.record_prod_candidate.assert_not_called()


def test_prod_candidate_unknown_agent_returns_403():
    """Fail-closed: the POSTed agent_id has no owning repo → the binding 403 (an unknown
    agent must never reach the store)."""
    conn_svc = MagicMock()
    conn_svc.get_connection.return_value = _conn(org="acme-org")
    proj_svc = _proj_svc(found=False)

    client = _build_client(
        conn_svc=conn_svc,
        build_svc=MagicMock(),
        claims=_claims(),
        proj_svc=proj_svc,
    )
    resp = client.post("/api/v1/builds/prod-candidate", json=_CANDIDATE_BODY)

    assert resp.status_code == 403
    assert resp.json()["detail"] == "Token repository does not own this agent"
    proj_svc.record_prod_candidate.assert_not_called()


def test_prod_candidate_ignores_body_supplied_actor_and_sha():
    """The marketplace attribution rule: a body-asserted `actor`/`sha` is IGNORED — only the
    token's claims are recorded, so a build-credential holder cannot claim another's merge."""
    conn_svc = MagicMock()
    conn_svc.get_connection.return_value = _conn()
    proj_svc = _proj_svc(repo_name="some-agent")

    client = _build_client(
        conn_svc=conn_svc,
        build_svc=MagicMock(),
        claims=_claims(actor="real-human", sha="realsha"),
        proj_svc=proj_svc,
    )
    resp = client.post(
        "/api/v1/builds/prod-candidate",
        json={**_CANDIDATE_BODY, "actor": "impersonated", "sha": "forged"},
    )

    assert resp.status_code == 202
    proj_svc.record_prod_candidate.assert_called_once_with(
        "agent-42",
        image_tag="agent-42-1a2b3c4",
        sha="realsha",
        actor="real-human",
    )


def test_prod_candidate_rejects_tag_not_prefixed_with_the_proven_agent_id():
    """The tag must name the agent the token was just proven to own. A tag with no
    `<agent_id>-` prefix is refused BEFORE the write (guard-before-write)."""
    conn_svc = MagicMock()
    conn_svc.get_connection.return_value = _conn()
    proj_svc = _proj_svc(repo_name="some-agent")

    client = _build_client(
        conn_svc=conn_svc,
        build_svc=MagicMock(),
        claims=_claims(),
        proj_svc=proj_svc,
    )
    resp = client.post(
        "/api/v1/builds/prod-candidate",
        json={**_CANDIDATE_BODY, "image_tag": "latest"},
    )

    assert resp.status_code == 403
    assert resp.json() == {
        "detail": "Image tag does not belong to this agent",
        "code": "runtime_build.tag_not_owned",
    }
    assert "latest" not in resp.text
    proj_svc.record_prod_candidate.assert_not_called()


def test_prod_candidate_rejects_another_agents_image_tag():
    """THE cross-agent attack. The tenant ECR repo is SHARED across all agents and the tag
    prefix is the only identity boundary in it (the buildspec re-derives AGENT_ID from the
    tag), so a token that legitimately passes all four token checks for its OWN agent must
    not be able to register a candidate pointing at ANOTHER agent's artifact — otherwise this
    agent's OWNER would promote a foreign image over the foreign agent's Terraform state."""
    conn_svc = MagicMock()
    conn_svc.get_connection.return_value = _conn()
    proj_svc = _proj_svc(repo_name="some-agent")

    client = _build_client(
        conn_svc=conn_svc,
        build_svc=MagicMock(),
        claims=_claims(),
        proj_svc=proj_svc,
    )
    resp = client.post(
        "/api/v1/builds/prod-candidate",
        json={**_CANDIDATE_BODY, "image_tag": "agent-99-abc1234"},
    )

    assert resp.status_code == 403
    assert resp.json()["detail"] == "Image tag does not belong to this agent"
    proj_svc.record_prod_candidate.assert_not_called()


@pytest.mark.parametrize(
    "bad_tag",
    [
        "agent-42-../../etc/passwd",  # path traversal flavour
        "agent-42- abc1234",  # whitespace (would word-split in the buildspec's shell)
        "agent-42-abc$(id)",  # shell metacharacters
        "agent-42-" + "a" * 200,  # beyond ECR's 128-char limit
    ],
)
def test_prod_candidate_rejects_illegal_tag_charset(bad_tag):
    """Correctly prefixed but outside ECR's own tag grammar ([A-Za-z0-9._-], max 128) → 403.
    The route is the only place this can be pinned: the buildspec is inline-capped and
    interpolates the derived DEPLOYMENT_ID unquoted."""
    conn_svc = MagicMock()
    conn_svc.get_connection.return_value = _conn()
    proj_svc = _proj_svc(repo_name="some-agent")

    client = _build_client(
        conn_svc=conn_svc,
        build_svc=MagicMock(),
        claims=_claims(),
        proj_svc=proj_svc,
    )
    resp = client.post(
        "/api/v1/builds/prod-candidate",
        json={**_CANDIDATE_BODY, "image_tag": bad_tag},
    )

    assert resp.status_code == 403
    assert resp.json()["detail"] == "Image tag does not belong to this agent"
    proj_svc.record_prod_candidate.assert_not_called()


def test_prod_candidate_accepts_the_workflows_real_tag_shape():
    """Availability: the tag the committed workflow actually produces
    (`{AGENT_ID}-{tree_sha[:7]}`) passes the new binding — the guard must not 403 every
    legitimate caller."""
    conn_svc = MagicMock()
    conn_svc.get_connection.return_value = _conn()
    proj_svc = _proj_svc(repo_name="some-agent")

    client = _build_client(
        conn_svc=conn_svc,
        build_svc=MagicMock(),
        claims=_claims(),
        proj_svc=proj_svc,
    )
    resp = client.post(
        "/api/v1/builds/prod-candidate",
        json={**_CANDIDATE_BODY, "image_tag": "agent-42-1a2b3c4"},
    )

    assert resp.status_code == 202
    proj_svc.record_prod_candidate.assert_called_once()


def test_prod_candidate_store_not_found_returns_404():
    """The store's own `agent_not_found` (the agent vanished between the binding read and the
    write) renders the project table's 404 with its fixed detail — never `str(err)`."""
    from services.project_errors import ProjectError

    conn_svc = MagicMock()
    conn_svc.get_connection.return_value = _conn()
    proj_svc = _proj_svc(repo_name="some-agent")
    proj_svc.record_prod_candidate.side_effect = ProjectError("store internals leak", kind="agent_not_found")

    client = _build_client(
        conn_svc=conn_svc,
        build_svc=MagicMock(),
        claims=_claims(),
        proj_svc=proj_svc,
    )
    resp = client.post("/api/v1/builds/prod-candidate", json=_CANDIDATE_BODY)

    assert resp.status_code == 404
    assert resp.json() == {"detail": "Agent not found", "code": "project.agent_not_found"}
    assert "store internals leak" not in resp.text


def test_prod_candidate_route_does_not_relax_the_runtime_prod_refusal():
    """Adding the candidate route must NOT make
    `/builds/runtime` accept `stage="prod"`."""
    conn_svc = MagicMock()
    conn_svc.get_connection.return_value = _conn()
    build_svc = MagicMock()

    client = _build_client(conn_svc=conn_svc, build_svc=build_svc, claims=_claims())
    resp = client.post("/api/v1/builds/runtime", json={**_BODY, "stage": "prod"})

    assert resp.status_code == 403
    assert resp.json() == {
        "detail": "prod deploys must be initiated from AGP",
        "code": "runtime_build.prod_from_agp_only",
    }
    build_svc.start_runtime_build.assert_not_called()


def test_builds_runtime_passes_stage_to_service():
    """A valid stage is threaded into start_runtime_build (tenant derived server-side)."""
    conn_svc = MagicMock()
    conn_svc.get_connection.return_value = _conn()
    build_svc = MagicMock()
    build_svc.start_runtime_build.return_value = "rt-build:2"

    client = _build_client(conn_svc=conn_svc, build_svc=build_svc, claims=_claims())
    resp = client.post("/api/v1/builds/runtime", json={**_BODY, "stage": "dev"})

    assert resp.status_code == 202
    build_svc.start_runtime_build.assert_called_once_with(
        agent_id="agent-42",
        image_tag="agent-42-abc",
        ecr_repo="123.dkr.ecr.us-east-1.amazonaws.com/agp",
        connection_id="conn-1",
        stage="dev",
        image_digest="",
    )


_GOOD_DIGEST = "sha256:" + "ab" * 32


def _proj_svc_with_trunk(trunk="main", **kw):
    """`_proj_svc` plus a `trunk_branch` on the resolved project, which the gate reads."""
    svc = _proj_svc(**kw)
    svc.get_project.return_value.project.trunk_branch = trunk
    return svc


def _post(body_extra=None, *, claims=None, proj_svc=None):
    """POST /builds/runtime with a passing binding, returning (response, proj_svc)."""
    conn_svc = MagicMock()
    conn_svc.get_connection.return_value = _conn()
    build_svc = MagicMock()
    build_svc.start_runtime_build.return_value = "rt-build:9"
    proj_svc = proj_svc if proj_svc is not None else _proj_svc_with_trunk()
    client = _build_client(
        conn_svc=conn_svc,
        build_svc=build_svc,
        claims=claims if claims is not None else _claims(),
        proj_svc=proj_svc,
    )
    resp = client.post("/api/v1/builds/runtime", json={**_BODY, **(body_extra or {})})
    return resp, proj_svc, build_svc


def test_the_posted_digest_reaches_the_build_service():
    """The field exists and is threaded through — the fix for the silent discard."""
    resp, _, build_svc = _post({"image_digest": _GOOD_DIGEST})
    assert resp.status_code == 202
    assert build_svc.start_runtime_build.call_args.kwargs["image_digest"] == _GOOD_DIGEST


def test_the_digest_is_not_silently_dropped_by_the_model():
    """The defect in its original form, asserted on the MODEL rather than through the route.

    `RuntimeBuildRequest(...).model_dump()` used to omit `image_digest` entirely because pydantic
    ignores extras — a 202 with the value gone. Pinned here so the field cannot be removed again
    without a test failing."""
    import api.routes.builds as builds_module

    dumped = builds_module.RuntimeBuildRequest(**_BODY, image_digest=_GOOD_DIGEST).model_dump()
    assert dumped["image_digest"] == _GOOD_DIGEST


@pytest.mark.parametrize(
    "digest",
    [
        "sha256:" + "a" * 63,  # truncated
        "sha256:" + "a" * 65,  # too long
        "sha256:" + "AB" * 32,  # uppercase — registries emit lowercase
        "ab" * 32,  # no algorithm prefix
        "sha512:" + "a" * 64,  # wrong algorithm
        "None",  # what the AWS CLI prints for a missing image
        _GOOD_DIGEST + "x",  # trailing junk
        _GOOD_DIGEST + "; echo hi",  # a shell metacharacter, bound for a docker reference
    ],
    ids=[
        "truncated",
        "too-long",
        "uppercase",
        "no-prefix",
        "wrong-algo",
        "literal-None",
        "trailing-junk",
        "metachar",
    ],
)
def test_a_malformed_digest_is_a_422(digest):
    """A malformed digest is a template/CI bug the workflow author must SEE, so it is a 422 rather
    than being quietly coerced. It must never reach the build service — this value ends up
    interpolated into a deployed image reference."""
    resp, _, build_svc = _post({"image_digest": digest})
    assert resp.status_code == 422
    build_svc.start_runtime_build.assert_not_called()


@pytest.mark.parametrize("digest", [None, "", "   "], ids=["absent", "empty", "whitespace"])
def test_a_missing_digest_is_accepted_as_the_legacy_path(digest):
    """A materialized repo carries a COMMITTED copy of its workflow, so repos created before this
    epic keep POSTing digest-less bodies. Requiring the field would 422 every one of them — turning
    a missing optimization into a total outage of the dev deploy path. Empty normalizes to the
    legacy path rather than 422 for the same reason a skipped upstream step can emit ""."""
    body = {} if digest is None else {"image_digest": digest}
    resp, _, build_svc = _post(body)
    assert resp.status_code == 202
    assert build_svc.start_runtime_build.call_args.kwargs["image_digest"] == ""


def test_a_trunk_push_registers_the_prod_candidate_with_its_digest():
    """The registration that replaced the deleted `candidate` job. Without it nothing writes the
    candidate block and every promotion is refused `no_prod_candidate` forever."""
    resp, proj_svc, _ = _post({"image_digest": _GOOD_DIGEST})
    assert resp.status_code == 202
    proj_svc.record_prod_candidate.assert_called_once_with(
        "agent-42",
        image_tag="agent-42-abc",
        image_digest=_GOOD_DIGEST,
        sha="abc",
        actor="merging-human",
    )


@pytest.mark.parametrize(
    "trunk,ref",
    [
        ("main", "refs/heads/feature/x"),
        ("main", "refs/heads/dev"),
        ("release", "refs/heads/main"),  # `main` is NOT the trunk here
        ("main", "refs/tags/v1.0.0"),
        ("main", "refs/heads/main-ish"),  # a prefix match must not count
    ],
    ids=["feature-branch", "dev-branch", "main-is-not-the-trunk", "a-tag", "prefix-lookalike"],
)
def test_a_NON_TRUNK_ref_registers_NO_candidate(trunk, ref):
    """**THE GOVERNANCE ASSERTION.** A candidate is what an owner's production approval attests to,
    so only a ref that went through the provider's review may produce one.

    `main-is-not-the-trunk` is the case a hardcoded literal would get wrong in the DANGEROUS
    direction: a project whose trunk is `release` would have every `main` push registered as
    promotable. `prefix-lookalike` pins the comparison as exact rather than a `startswith`.

    The build itself still runs — a feature branch may legitimately deploy to dev; it simply must
    not become approvable for production."""
    resp, proj_svc, build_svc = _post(
        {"image_digest": _GOOD_DIGEST},
        claims=_claims(ref=ref),
        proj_svc=_proj_svc_with_trunk(trunk),
    )
    assert resp.status_code == 202
    build_svc.start_runtime_build.assert_called_once()
    proj_svc.record_prod_candidate.assert_not_called()


def test_a_non_main_trunk_DOES_register_on_its_own_trunk():
    """The other half of the gate: reading the project's `trunk_branch` (D-B5) rather than a `main`
    literal is what makes a non-`main` project promotable at all. Without this the Promote button
    would go quiet with no error — this epic's signature defect."""
    resp, proj_svc, _ = _post(
        {"image_digest": _GOOD_DIGEST},
        claims=_claims(ref="refs/heads/release"),
        proj_svc=_proj_svc_with_trunk("release"),
    )
    assert resp.status_code == 202
    proj_svc.record_prod_candidate.assert_called_once()


def test_a_project_predating_the_trunk_setting_falls_back_to_main():
    """A pre-D-B5 project row carries no `trunk_branch`. It must still register on `main` rather
    than becoming silently unpromotable."""
    proj_svc = _proj_svc()
    project = MagicMock(spec=["connection_id"])
    project.connection_id = "conn-1"
    proj_svc.get_project.return_value.project = project
    resp, proj_svc, _ = _post({"image_digest": _GOOD_DIGEST}, proj_svc=proj_svc)
    assert resp.status_code == 202
    proj_svc.record_prod_candidate.assert_called_once()


def test_a_candidate_tag_naming_another_agent_is_refused():
    """The tag↔agent binding runs on this path too. The tenant ECR repo is SHARED by every
    materialized agent, so the tag PREFIX is the only agent-identity boundary in the registry — and
    this value becomes what an owner of THIS repo approves for production. A foreign tag must be a
    403, never a registered candidate."""
    resp, proj_svc, _ = _post({"image_tag": "agent-OTHER-abc", "image_digest": _GOOD_DIGEST})
    assert resp.status_code == 403
    proj_svc.record_prod_candidate.assert_not_called()


def test_a_foreign_tag_STARTS_NO_BUILD_and_writes_NO_HISTORY():
    """THE ordering assertion, and a status code alone would not have caught the defect it guards.

    When the tag binding lived inside the post-build registrar, this exact request returned 403
    **after** `StartBuild` had run and a delivery row had been appended — the route answering
    "refused" about a build already in motion, with history recording it. `assert_not_called()` on
    BOTH side effects is what distinguishes "refused" from "refused, but did it anyway"; the
    status-code check above passes in either world."""
    resp, proj_svc, build_svc = _post({"image_tag": "agent-OTHER-abc", "image_digest": _GOOD_DIGEST})
    assert resp.status_code == 403
    build_svc.start_runtime_build.assert_not_called()
    proj_svc.append_deployment.assert_not_called()
    proj_svc.record_prod_candidate.assert_not_called()


def test_every_refusal_precedes_every_side_effect():
    """The general rule the case above is one instance of, over all four refusals this route makes.

    A refusal that lands after a side effect is not a refusal — it is a misreport. Parametrizing
    across the refusals keeps a NEW check from being added in the wrong place (after `StartBuild`)
    and still looking correct from the response alone."""
    for label, body_extra, claims, expected in (
        ("bad stage", {"stage": "uat"}, None, 422),
        ("prod over OIDC", {"stage": "prod"}, None, 403),
        ("foreign tag", {"image_tag": "agent-OTHER-abc"}, None, 403),
        ("malformed digest", {"image_digest": "sha256:nope"}, None, 422),
    ):
        resp, proj_svc, build_svc = _post(body_extra, claims=claims)
        assert resp.status_code == expected, label
        build_svc.start_runtime_build.assert_not_called()
        proj_svc.append_deployment.assert_not_called()
        proj_svc.record_prod_candidate.assert_not_called()


def test_a_registration_failure_does_not_fail_the_running_build():
    """BEST-EFFORT, like the delivery-row append: the build has already started by the time this
    runs, so a store fault must not turn a live deploy into a 500 the workflow retries. An
    unregistered candidate is a Promote button that stays quiet until the next merge; a wrong HTTP
    answer here would misreport what happened to a runtime."""
    proj_svc = _proj_svc_with_trunk()
    proj_svc.record_prod_candidate.side_effect = RuntimeError("ddb is having a day")
    resp, _, build_svc = _post({"image_digest": _GOOD_DIGEST}, proj_svc=proj_svc)
    assert resp.status_code == 202
    assert resp.json() == {"build_id": "rt-build:9", "status": "started"}
    build_svc.start_runtime_build.assert_called_once()


def test_a_refused_build_registers_no_candidate():
    """Registration sits AFTER every refusal, so a rejected stage leaves no approvable artifact
    behind. A prod-stage POST is refused over OIDC — it must not register either."""
    resp, proj_svc, build_svc = _post({"stage": "prod", "image_digest": _GOOD_DIGEST})
    assert resp.status_code == 403
    build_svc.start_runtime_build.assert_not_called()
    proj_svc.record_prod_candidate.assert_not_called()


_SHA40 = "0123456789abcdef0123456789abcdef01234567"


def _post_with_kind(build_kind, body_extra=None, *, claims=None):
    """POST /builds/runtime with the build service reporting a resolved `build_kind`.

    `build_kind=None` means the chain resolved to nothing (an agent with no chain) — which must behave
    exactly like `"container"`."""
    conn_svc = MagicMock()
    conn_svc.get_connection.return_value = _conn()
    build_svc = MagicMock()
    build_svc.start_runtime_build.return_value = "rt-build:9"
    if build_kind is None:
        build_svc.resolve_infra_template.return_value = None
    else:
        resolved = MagicMock(spec=["build_kind", "iac_type"])
        resolved.build_kind = build_kind
        resolved.iac_type = "agentcore_harness" if build_kind == "config" else "agentcore_runtime"
        build_svc.resolve_infra_template.return_value = resolved
    proj_svc = _proj_svc_with_trunk()
    client = _build_client(
        conn_svc=conn_svc,
        build_svc=build_svc,
        claims=claims if claims is not None else _claims(),
        proj_svc=proj_svc,
    )
    resp = client.post("/api/v1/builds/runtime", json={**_BODY, **(body_extra or {})})
    return resp, proj_svc, build_svc


def test_a_config_build_accepts_the_harness_workflows_bare_sha_tag():
    """The exact tag the harness workflow posts must reach the build."""
    resp, _, build_svc = _post_with_kind("config", {"image_tag": _SHA40, "ecr_repo": ""})
    assert resp.status_code == 202
    build_svc.start_runtime_build.assert_called_once()
    assert build_svc.start_runtime_build.call_args.kwargs["image_tag"] == _SHA40


@pytest.mark.parametrize(
    "bad_tag",
    [
        pytest.param(_SHA40[:39], id="too_short"),
        pytest.param(_SHA40 + "0", id="too_long"),
        pytest.param(_SHA40[:-1] + "Z", id="not_hex"),
        pytest.param(_SHA40.upper(), id="uppercase"),
        pytest.param("agent-42-abc1234", id="the_container_tag_shape"),
        pytest.param(f" {_SHA40}", id="leading_whitespace"),
    ],
)
def test_a_config_build_still_refuses_a_tag_that_is_not_a_bare_sha(bad_tag):
    """The config branch is a DIFFERENT rule, not an absent one. Anything but 40 lowercase hex is
    refused — including the container path's own tag shape, since accepting both would make the
    gate a union and reopen the cross-agent hole on whichever path is weaker."""
    resp, proj_svc, build_svc = _post_with_kind("config", {"image_tag": bad_tag, "ecr_repo": ""})
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Image tag does not belong to this agent"
    assert bad_tag.strip() not in resp.text
    build_svc.start_runtime_build.assert_not_called()
    proj_svc.append_deployment.assert_not_called()
    proj_svc.record_prod_candidate.assert_not_called()


@pytest.mark.parametrize("kind", ["container", None])
def test_the_container_prefix_rule_is_unchanged(kind):
    """REGRESSION PIN, over both the resolved-container case and the unresolvable one. The prefix
    rule is the only agent-identity boundary in a shared tenant registry, so it must survive this
    generalisation byte for byte — a foreign tag stays a 403 and a bare sha is NOT accepted here."""
    resp, _, build_svc = _post_with_kind(kind, {"image_tag": "agent-42-abc1234"})
    assert resp.status_code == 202

    resp, proj_svc, build_svc = _post_with_kind(kind, {"image_tag": "agent-OTHER-abc1234"})
    assert resp.status_code == 403
    build_svc.start_runtime_build.assert_not_called()

    resp, proj_svc, build_svc = _post_with_kind(kind, {"image_tag": _SHA40})
    assert resp.status_code == 403
    build_svc.start_runtime_build.assert_not_called()
    proj_svc.append_deployment.assert_not_called()


def test_a_body_supplied_iac_type_is_dropped_by_the_model():
    """`iac_type` is resolved from the CHAIN, never accepted from the caller.

    The model declares no such field and pydantic ignores extras, so a sent value is discarded —
    asserted here rather than assumed, because "the model happens not to declare it" is exactly
    the silence that regresses the moment someone adds a passthrough field. The route-level half
    is that the resolved kind still decides the gate: the body below claims `agentcore_harness`
    while the chain says container, and the container prefix rule still applies."""
    import api.routes.builds as builds_module

    dumped = builds_module.RuntimeBuildRequest.model_validate(
        {**_BODY, "iac_type": "agentcore_harness", "build_kind": "config"}
    ).model_dump()
    assert "iac_type" not in dumped
    assert "build_kind" not in dumped

    resp, _, build_svc = _post_with_kind(
        "container",
        {"image_tag": _SHA40, "iac_type": "agentcore_harness", "build_kind": "config"},
    )
    assert resp.status_code == 403
    build_svc.start_runtime_build.assert_not_called()


def test_a_resolver_fault_does_not_fail_the_build_and_keeps_the_container_rule():
    """The resolver reads three catalog rows, and a store fault there must not 500 the deploy
    path. It degrades to the CONTAINER rule — the strictest of the two and the one every
    chainless agent needs — rather than to "no rule at all", which would turn a transient DDB
    blip into an open tag gate."""
    conn_svc = MagicMock()
    conn_svc.get_connection.return_value = _conn()
    build_svc = MagicMock()
    build_svc.start_runtime_build.return_value = "rt-build:9"
    build_svc.resolve_infra_template.side_effect = RuntimeError("ddb is having a day")
    proj_svc = _proj_svc_with_trunk()
    client = _build_client(
        conn_svc=conn_svc,
        build_svc=build_svc,
        claims=_claims(),
        proj_svc=proj_svc,
    )

    ok = client.post("/api/v1/builds/runtime", json={**_BODY, "image_tag": "agent-42-abc1234"})
    assert ok.status_code == 202

    refused = client.post("/api/v1/builds/runtime", json={**_BODY, "image_tag": _SHA40})
    assert refused.status_code == 403


def test_prod_candidate_keeps_the_container_rule_unconditionally():
    """`/prod-candidate` is byte-unchanged. A config agent has no image, so it can never have a
    prod candidate — the route keeps the container default rather than growing a branch for a
    path that cannot reach it."""
    conn_svc = MagicMock()
    conn_svc.get_connection.return_value = _conn()
    proj_svc = _proj_svc(repo_name="some-agent")
    client = _build_client(
        conn_svc=conn_svc,
        build_svc=MagicMock(),
        claims=_claims(),
        proj_svc=proj_svc,
    )
    resp = client.post(
        "/api/v1/builds/prod-candidate",
        json={**_CANDIDATE_BODY, "image_tag": _SHA40},
    )
    assert resp.status_code == 403
    proj_svc.record_prod_candidate.assert_not_called()


_MCP_BODY = {
    "mcp_server_id": "mcp-1",
    "image_tag": _SHA40,
    "ecr_repo": "",
    "connection_id": "conn-1",
    "stage": "dev",
}


def _mcp_proj_svc(*, repo_name="claims-mcp", found=True):
    svc = _proj_svc_with_trunk()
    repo = MagicMock()
    repo.name = repo_name
    repo.id = "repo-mcp-1"
    repo.project_id = "proj-1"
    svc.find_repository_by_mcp_server_id.return_value = repo if found else None
    return svc


def _post_mcp(body=None, *, claims=None, proj_svc=None):
    conn_svc = MagicMock()
    conn_svc.get_connection.return_value = _conn()
    build_svc = MagicMock()
    build_svc.start_runtime_build.return_value = "gw-build:1"
    proj_svc = proj_svc if proj_svc is not None else _mcp_proj_svc()
    client = _build_client(
        conn_svc=conn_svc,
        build_svc=build_svc,
        claims=claims if claims is not None else _claims(repo="claims-mcp"),
        proj_svc=proj_svc,
    )
    resp = client.post("/api/v1/builds/runtime", json=body if body is not None else _MCP_BODY)
    return resp, proj_svc, build_svc


def test_an_mcp_build_starts_with_the_mcp_server_id():
    resp, proj_svc, build_svc = _post_mcp()
    assert resp.status_code == 202
    assert resp.json() == {"build_id": "gw-build:1", "status": "started"}
    build_svc.start_runtime_build.assert_called_once_with(
        mcp_server_id="mcp-1",
        image_tag=_SHA40,
        ecr_repo="",
        connection_id="conn-1",
        stage="dev",
        image_digest="",
        expected_build_kind="config",
    )
    proj_svc.find_repository_by_mcp_server_id.assert_called_with("mcp-1")
    import inspect

    from services.project_service import ProjectService

    kw = proj_svc.append_deployment.call_args.kwargs
    inspect.signature(ProjectService.append_deployment).bind(None, **kw)
    assert kw["repo_id"] == "repo-mcp-1" and kw["mcp_server_id"] == "mcp-1"
    assert "agent_id" not in kw
    proj_svc.record_prod_candidate.assert_not_called()


@pytest.mark.parametrize(
    "ids",
    [{}, {"agent_id": "agent-42", "mcp_server_id": "mcp-1"}],
    ids=["neither", "both"],
)
def test_the_body_must_name_exactly_one_artifact(ids):
    body = {k: v for k, v in _MCP_BODY.items() if k != "mcp_server_id"}
    resp, _, build_svc = _post_mcp({**body, **ids})
    assert resp.status_code == 422
    build_svc.start_runtime_build.assert_not_called()


def test_an_mcp_build_refuses_a_token_whose_repo_does_not_own_the_mcp():
    resp, _, build_svc = _post_mcp(proj_svc=_mcp_proj_svc(repo_name="other-repo"))
    assert resp.status_code == 403
    assert resp.json() == {
        "detail": "Token repository does not own this MCP server",
        "code": "runtime_build.token_not_mcp_owner",
    }
    build_svc.start_runtime_build.assert_not_called()


def test_an_mcp_build_for_an_unknown_mcp_is_403():
    resp, _, build_svc = _post_mcp(proj_svc=_mcp_proj_svc(found=False))
    assert resp.status_code == 403
    build_svc.start_runtime_build.assert_not_called()


def test_an_mcp_build_requires_a_bare_commit_sha_tag():
    resp, _, build_svc = _post_mcp({**_MCP_BODY, "image_tag": "mcp-1-abc1234"})
    assert resp.status_code == 403
    build_svc.start_runtime_build.assert_not_called()


def test_an_mcp_prod_build_is_refused_over_oidc():
    resp, _, build_svc = _post_mcp({**_MCP_BODY, "stage": "prod"})
    assert resp.status_code == 403
    assert resp.json() == {
        "detail": "prod deploys must be initiated from AGP",
        "code": "runtime_build.prod_from_agp_only",
    }
    build_svc.start_runtime_build.assert_not_called()


def test_an_in_flight_mcp_build_is_409():
    conn_svc = MagicMock()
    conn_svc.get_connection.return_value = _conn()
    build_svc = MagicMock()
    build_svc.start_runtime_build.side_effect = RuntimeBuildError("busy", kind="conflict")
    client = _build_client(
        conn_svc=conn_svc,
        build_svc=build_svc,
        claims=_claims(repo="claims-mcp"),
        proj_svc=_mcp_proj_svc(),
    )
    resp = client.post("/api/v1/builds/runtime", json=_MCP_BODY)
    assert resp.status_code == 409


def test_an_agent_build_history_row_binds_to_the_real_signature():
    import inspect

    from services.project_service import ProjectService

    resp, proj_svc, _ = _post()
    assert resp.status_code == 202
    kw = proj_svc.append_deployment.call_args.kwargs
    inspect.signature(ProjectService.append_deployment).bind(None, **kw)
    assert kw["agent_id"] == "agent-42" and "mcp_server_id" not in kw


def test_an_unbuildable_mcp_name_is_a_422_with_no_history_row():
    conn_svc = MagicMock()
    conn_svc.get_connection.return_value = _conn()
    build_svc = MagicMock()
    build_svc.start_runtime_build.side_effect = RuntimeBuildError("bad name", kind="invalid_name")
    proj_svc = _mcp_proj_svc()
    client = _build_client(
        conn_svc=conn_svc,
        build_svc=build_svc,
        claims=_claims(repo="claims-mcp"),
        proj_svc=proj_svc,
    )
    resp = client.post("/api/v1/builds/runtime", json=_MCP_BODY)
    assert resp.status_code == 422
    assert resp.json() == {"detail": "MCP server name is not buildable", "code": "runtime_build.invalid_name"}
    proj_svc.append_deployment.assert_not_called()
