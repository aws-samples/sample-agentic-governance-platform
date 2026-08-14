"""Build-trigger route tests (E22/T6).

Uses a FastAPI dependency-override for `verify_github_oidc` (so no live GitHub JWKS), and
patches the module-level `_conn_svc`/`_build_svc` singletons with fakes (no live AWS).
Covers: happy path (202 + build_id), org mismatch → 403, unknown connection → 404, and a
StartBuild failure → 502.
"""

from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.security_github_oidc import GitHubActionsClaims
from models.connection import AuthType, Connection, ConnStatus, Provider
from services.connection_service import ConnectionError
from services.runtime_build_service import RuntimeBuildError


@pytest.fixture(autouse=True)
def reset_modules():
    import sys
    for mod in ["api.routes.builds", "core.config"]:
        sys.modules.pop(mod, None)
    yield


def _conn(org="acme-org"):
    return Connection(
        id="conn-1", provider=Provider.GITHUB, org=org, base_url=None,
        auth_type=AuthType.PAT, status=ConnStatus.CONNECTED,
        secret_arn="arn:aws:secretsmanager:us-east-1:111:secret:agp/git/conn-1",
        has_secret=True, created_by="a@b.com", created_at="t", updated_at="t",
    )


def _proj_svc(*, repo_name="some-agent", project_conn_id="conn-1", found=True):
    """Fake ProjectService for the E25/I1 agent_id↔token.repository binding. Returns a repo
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
    import api.routes.builds as builds_module

    builds_module._conn_svc = conn_svc
    builds_module._build_svc = build_svc
    # E25/I1: the builds route resolves the repo owning the POSTed agent_id via the project
    # service to bind it to the token's proven `repository`. Default fake passes the binding.
    builds_module._project_svc = proj_svc if proj_svc is not None else _proj_svc()

    app = FastAPI()
    app.include_router(builds_module.router, prefix="/api/v1")
    # Key the override off the SAME function object the route's Depends resolved (the
    # builds module re-imports it fresh after module cache resets, so a top-level import
    # here would be a different object and the override would silently miss → 403).
    app.dependency_overrides[builds_module.verify_github_oidc] = lambda: claims
    return TestClient(app)


def _claims(owner="acme-org", repo="some-agent", ref="refs/heads/main", sha="abc",
            actor="merging-human", event_name="push"):
    return GitHubActionsClaims(
        repository=f"{owner}/{repo}", repository_owner=owner,
        sub="repo:x", ref=ref, sha=sha, workflow="build",
        actor=actor, event_name=event_name,
    )


_BODY = {
    "agent_id": "agent-42", "image_tag": "agent-42-abc",
    "ecr_repo": "123.dkr.ecr.us-east-1.amazonaws.com/agp", "connection_id": "conn-1",
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
        agent_id="agent-42", image_tag="agent-42-abc",
        ecr_repo="123.dkr.ecr.us-east-1.amazonaws.com/agp", connection_id="conn-1",
        stage="dev",
        # E28B/T4: `_BODY` carries no digest, so the legacy tag-only path passes "" — asserted
        # explicitly rather than dropped, because an ABSENT kwarg and an empty one reach the
        # buildspec identically and only one of them is intentional.
        image_digest="",
    )


def test_org_mismatch_returns_403():
    conn_svc = MagicMock()
    conn_svc.get_connection.return_value = _conn(org="acme-org")
    build_svc = MagicMock()

    # Token minted for a different org than the resolved connection.
    client = _build_client(conn_svc=conn_svc, build_svc=build_svc, claims=_claims(owner="evil-org"))
    resp = client.post("/api/v1/builds/runtime", json=_BODY)

    assert resp.status_code == 403
    build_svc.start_runtime_build.assert_not_called()


def test_unknown_connection_returns_404():
    conn_svc = MagicMock()
    conn_svc.get_connection.side_effect = ConnectionError("nope", kind="not_found")
    build_svc = MagicMock()

    client = _build_client(conn_svc=conn_svc, build_svc=build_svc, claims=_claims())
    resp = client.post("/api/v1/builds/runtime", json=_BODY)

    assert resp.status_code == 404
    build_svc.start_runtime_build.assert_not_called()


def test_start_build_failure_returns_502():
    conn_svc = MagicMock()
    conn_svc.get_connection.return_value = _conn()
    build_svc = MagicMock()
    build_svc.start_runtime_build.side_effect = RuntimeBuildError("start failed")

    client = _build_client(conn_svc=conn_svc, build_svc=build_svc, claims=_claims())
    resp = client.post("/api/v1/builds/runtime", json=_BODY)

    assert resp.status_code == 502
    assert resp.json()["detail"]["status"] == "failed_to_start"


def test_builds_runtime_rejects_bad_stage():
    """A stage outside ("dev","prod") → 422; the build service is never called."""
    conn_svc = MagicMock()
    conn_svc.get_connection.return_value = _conn()
    build_svc = MagicMock()

    client = _build_client(conn_svc=conn_svc, build_svc=build_svc, claims=_claims())
    resp = client.post("/api/v1/builds/runtime", json={**_BODY, "stage": "staging"})

    assert resp.status_code == 422
    build_svc.start_runtime_build.assert_not_called()


def test_builds_runtime_rejects_agent_not_owned_by_token_repo():
    """E25/I1: token.repository="acme-org/repo-A" but the POSTed agent_id belongs to
    "acme-org/repo-B" → 403 (a token cannot build another repo's agent within its own org)."""
    conn_svc = MagicMock()
    conn_svc.get_connection.return_value = _conn(org="acme-org")
    build_svc = MagicMock()
    # The agent_id's owning repo is "repo-B", but the token was minted for "repo-A".
    proj_svc = _proj_svc(repo_name="repo-B")

    client = _build_client(
        conn_svc=conn_svc, build_svc=build_svc,
        claims=_claims(owner="acme-org", repo="repo-A"), proj_svc=proj_svc,
    )
    resp = client.post("/api/v1/builds/runtime", json=_BODY)

    assert resp.status_code == 403
    build_svc.start_runtime_build.assert_not_called()


def test_builds_runtime_allows_matching_repo():
    """E25/I1: token.repository="acme-org/my-agent" and the agent_id's owning repo is
    "my-agent" in org "acme-org" → the binding passes and the build proceeds (202)."""
    conn_svc = MagicMock()
    conn_svc.get_connection.return_value = _conn(org="acme-org")
    build_svc = MagicMock()
    build_svc.start_runtime_build.return_value = "rt-build:3"
    proj_svc = _proj_svc(repo_name="my-agent")

    client = _build_client(
        conn_svc=conn_svc, build_svc=build_svc,
        claims=_claims(owner="acme-org", repo="my-agent"), proj_svc=proj_svc,
    )
    resp = client.post("/api/v1/builds/runtime", json=_BODY)

    assert resp.status_code == 202
    build_svc.start_runtime_build.assert_called_once()


def test_builds_runtime_allows_case_mismatched_repo():
    """M1: GitHub org/repo logins are case-insensitive. The OIDC token carries canonical
    case ("Acme/My-Agent") while our store may hold a divergent case ("acme/my-agent").
    Both the org-match and the E25/I1 binding must case-fold so a legitimate build is not
    spuriously 403'd (availability; never a false-accept — both operands are trusted)."""
    conn_svc = MagicMock()
    conn_svc.get_connection.return_value = _conn(org="acme")  # stored lowercase
    build_svc = MagicMock()
    build_svc.start_runtime_build.return_value = "rt-build:4"
    proj_svc = _proj_svc(repo_name="my-agent")  # stored lowercase

    # Token minted with GitHub's canonical (differing) case.
    client = _build_client(
        conn_svc=conn_svc, build_svc=build_svc,
        claims=_claims(owner="Acme", repo="My-Agent"), proj_svc=proj_svc,
    )
    resp = client.post("/api/v1/builds/runtime", json=_BODY)

    assert resp.status_code == 202
    build_svc.start_runtime_build.assert_called_once()


def test_builds_runtime_unknown_agent_returns_403():
    """M2 (fail-closed): the POSTed agent_id has no owning repo in our store
    (find_repository_by_agent_id → None) → 403, and the build service is never called."""
    conn_svc = MagicMock()
    conn_svc.get_connection.return_value = _conn(org="acme-org")
    build_svc = MagicMock()
    proj_svc = _proj_svc(found=False)  # unknown agent → no owning repo

    client = _build_client(
        conn_svc=conn_svc, build_svc=build_svc, claims=_claims(), proj_svc=proj_svc,
    )
    resp = client.post("/api/v1/builds/runtime", json=_BODY)

    assert resp.status_code == 403
    build_svc.start_runtime_build.assert_not_called()


def test_builds_runtime_missing_project_returns_403():
    """M2 (fail-closed): the owning repo exists but its project has vanished
    (get_project → None) → repo_org stays None → 403, service not called."""
    conn_svc = MagicMock()
    conn_svc.get_connection.return_value = _conn(org="acme-org")
    build_svc = MagicMock()
    proj_svc = _proj_svc(repo_name="some-agent")
    proj_svc.get_project.return_value = None

    client = _build_client(
        conn_svc=conn_svc, build_svc=build_svc, claims=_claims(), proj_svc=proj_svc,
    )
    resp = client.post("/api/v1/builds/runtime", json=_BODY)

    assert resp.status_code == 403
    build_svc.start_runtime_build.assert_not_called()


def test_builds_runtime_repo_connection_error_returns_403():
    """M2 (fail-closed): resolving the repo's own project connection raises ConnectionError
    (e.g. connection removed) → repo_org stays None → 403 (not 500), service not called."""
    conn_svc = MagicMock()
    # First call (body.connection_id) succeeds; the repo's own connection lookup fails.
    conn_svc.get_connection.side_effect = [
        _conn(org="acme-org"),
        ConnectionError("gone", kind="not_found"),
    ]
    build_svc = MagicMock()
    proj_svc = _proj_svc(repo_name="some-agent")

    client = _build_client(
        conn_svc=conn_svc, build_svc=build_svc, claims=_claims(), proj_svc=proj_svc,
    )
    resp = client.post("/api/v1/builds/runtime", json=_BODY)

    assert resp.status_code == 403
    build_svc.start_runtime_build.assert_not_called()


def test_oidc_prod_stage_is_refused():
    """E27/T9: the OIDC path has NO human principal, so an OWNER check is impossible here.
    Refusing `stage="prod"` is what makes the promote route's OWNER gate real rather than
    advisory — otherwise anyone who can push a workflow bypasses it entirely."""
    conn_svc = MagicMock()
    conn_svc.get_connection.return_value = _conn()
    build_svc = MagicMock()

    client = _build_client(conn_svc=conn_svc, build_svc=build_svc, claims=_claims())
    resp = client.post("/api/v1/builds/runtime", json={**_BODY, "stage": "prod"})

    assert resp.status_code == 403
    assert resp.json()["detail"] == "prod deploys must be initiated from AGP"
    build_svc.start_runtime_build.assert_not_called()


def test_oidc_dev_stage_still_works():
    """E27/T9: dev behaviour is UNTOUCHED by the prod refusal."""
    conn_svc = MagicMock()
    conn_svc.get_connection.return_value = _conn()
    build_svc = MagicMock()
    build_svc.start_runtime_build.return_value = "rt-build:9"

    client = _build_client(conn_svc=conn_svc, build_svc=build_svc, claims=_claims())
    resp = client.post("/api/v1/builds/runtime", json={**_BODY, "stage": "dev"})

    assert resp.status_code == 202
    build_svc.start_runtime_build.assert_called_once()


def test_prod_refusal_happens_after_binding_checks():
    """E27/T9 ordering: a foreign-repo token must still get the BINDING 403, not the stage
    message. The refusal sits AFTER the binding checks so an attacker probing with a foreign
    token learns nothing about stage handling from the ordering."""
    conn_svc = MagicMock()
    conn_svc.get_connection.return_value = _conn(org="acme-org")
    # The agent_id's owning repo is "repo-B" but the token was minted for "repo-A".
    proj_svc = _proj_svc(repo_name="repo-B")
    build_svc = MagicMock()

    client = _build_client(
        conn_svc=conn_svc, build_svc=build_svc,
        claims=_claims(owner="acme-org", repo="repo-A"), proj_svc=proj_svc,
    )
    resp = client.post("/api/v1/builds/runtime", json={**_BODY, "stage": "prod"})

    assert resp.status_code == 403
    assert resp.json()["detail"] != "prod deploys must be initiated from AGP"
    assert resp.json()["detail"] == "Token repository does not own this agent"
    build_svc.start_runtime_build.assert_not_called()


_CANDIDATE_BODY = {
    "agent_id": "agent-42", "image_tag": "agent-42-1a2b3c4",
    "ecr_repo": "some.dkr.ecr.us-east-1.amazonaws.com/agp", "connection_id": "conn-1",
}


def test_prod_candidate_happy_path_records_oidc_proven_actor_and_sha():
    """E27A/T6: a merge to `main` registers the candidate — 202 + the tag from the BODY but
    the actor and sha from the VALIDATED TOKEN (never body-asserted)."""
    conn_svc = MagicMock()
    conn_svc.get_connection.return_value = _conn()
    proj_svc = _proj_svc(repo_name="some-agent")

    client = _build_client(
        conn_svc=conn_svc, build_svc=MagicMock(),
        claims=_claims(actor="merging-human", sha="deadbeefcafe"), proj_svc=proj_svc,
    )
    resp = client.post("/api/v1/builds/prod-candidate", json=_CANDIDATE_BODY)

    assert resp.status_code == 202
    assert resp.json() == {"status": "registered"}
    proj_svc.record_prod_candidate.assert_called_once_with(
        "agent-42", image_tag="agent-42-1a2b3c4", sha="deadbeefcafe", actor="merging-human",
    )


def test_prod_candidate_rejects_non_main_ref():
    """`main` IS the prod candidate (design §1) — a dev-branch token gets 403 and nothing
    is recorded."""
    conn_svc = MagicMock()
    conn_svc.get_connection.return_value = _conn()
    proj_svc = _proj_svc(repo_name="some-agent")

    client = _build_client(
        conn_svc=conn_svc, build_svc=MagicMock(),
        claims=_claims(ref="refs/heads/dev"), proj_svc=proj_svc,
    )
    resp = client.post("/api/v1/builds/prod-candidate", json=_CANDIDATE_BODY)

    assert resp.status_code == 403
    assert resp.json()["detail"] == "prod candidates come from main only"
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
    # The agent_id's owning repo is "repo-B" but the token was minted for "repo-A".
    proj_svc = _proj_svc(repo_name="repo-B")

    client = _build_client(
        conn_svc=conn_svc, build_svc=MagicMock(),
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
        conn_svc=conn_svc, build_svc=MagicMock(), claims=_claims(), proj_svc=proj_svc,
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
        conn_svc=conn_svc, build_svc=MagicMock(), claims=_claims(), proj_svc=proj_svc,
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
        conn_svc=conn_svc, build_svc=MagicMock(),
        claims=_claims(actor="real-human", sha="realsha"), proj_svc=proj_svc,
    )
    resp = client.post(
        "/api/v1/builds/prod-candidate",
        json={**_CANDIDATE_BODY, "actor": "impersonated", "sha": "forged"},
    )

    assert resp.status_code == 202
    proj_svc.record_prod_candidate.assert_called_once_with(
        "agent-42", image_tag="agent-42-1a2b3c4", sha="realsha", actor="real-human",
    )


def test_prod_candidate_rejects_tag_not_prefixed_with_the_proven_agent_id():
    """The tag must name the agent the token was just proven to own. A tag with no
    `<agent_id>-` prefix is refused BEFORE the write (guard-before-write)."""
    conn_svc = MagicMock()
    conn_svc.get_connection.return_value = _conn()
    proj_svc = _proj_svc(repo_name="some-agent")

    client = _build_client(
        conn_svc=conn_svc, build_svc=MagicMock(), claims=_claims(), proj_svc=proj_svc,
    )
    resp = client.post(
        "/api/v1/builds/prod-candidate",
        json={**_CANDIDATE_BODY, "image_tag": "latest"},
    )

    assert resp.status_code == 403
    assert resp.json()["detail"] == "Image tag does not belong to this agent"
    assert "latest" not in resp.text  # the offending tag is never echoed back
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
        conn_svc=conn_svc, build_svc=MagicMock(), claims=_claims(), proj_svc=proj_svc,
    )
    # A wholly valid token/repo/agent/ref for agent-42, but the tag names agent-99.
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
        "agent-42-../../etc/passwd",   # path traversal flavour
        "agent-42- abc1234",           # whitespace (would word-split in the buildspec's shell)
        "agent-42-abc$(id)",           # shell metacharacters
        "agent-42-" + "a" * 200,       # beyond ECR's 128-char limit
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
        conn_svc=conn_svc, build_svc=MagicMock(), claims=_claims(), proj_svc=proj_svc,
    )
    resp = client.post(
        "/api/v1/builds/prod-candidate", json={**_CANDIDATE_BODY, "image_tag": bad_tag},
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
        conn_svc=conn_svc, build_svc=MagicMock(), claims=_claims(), proj_svc=proj_svc,
    )
    resp = client.post(
        "/api/v1/builds/prod-candidate",
        json={**_CANDIDATE_BODY, "image_tag": "agent-42-1a2b3c4"},
    )

    assert resp.status_code == 202
    proj_svc.record_prod_candidate.assert_called_once()


def test_prod_candidate_store_not_found_returns_404():
    """The store's own `not_found` (the agent vanished between the binding read and the
    write) maps to 404 with a FIXED detail — never `str(err)`."""
    from services.project_service import ProjectError

    conn_svc = MagicMock()
    conn_svc.get_connection.return_value = _conn()
    proj_svc = _proj_svc(repo_name="some-agent")
    proj_svc.record_prod_candidate.side_effect = ProjectError(
        "store internals leak", kind="not_found"
    )

    client = _build_client(
        conn_svc=conn_svc, build_svc=MagicMock(), claims=_claims(), proj_svc=proj_svc,
    )
    resp = client.post("/api/v1/builds/prod-candidate", json=_CANDIDATE_BODY)

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Agent not found"
    assert "store internals leak" not in resp.text


def test_prod_candidate_route_does_not_relax_the_runtime_prod_refusal():
    """E27A regression on the E27/T9 invariant: adding the candidate route must NOT make
    `/builds/runtime` accept `stage="prod"`."""
    conn_svc = MagicMock()
    conn_svc.get_connection.return_value = _conn()
    build_svc = MagicMock()

    client = _build_client(conn_svc=conn_svc, build_svc=build_svc, claims=_claims())
    resp = client.post("/api/v1/builds/runtime", json={**_BODY, "stage": "prod"})

    assert resp.status_code == 403
    assert resp.json()["detail"] == "prod deploys must be initiated from AGP"
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
        agent_id="agent-42", image_tag="agent-42-abc",
        ecr_repo="123.dkr.ecr.us-east-1.amazonaws.com/agp", connection_id="conn-1",
        stage="dev", image_digest="",
    )


# --------------------------------------------------------------------------- #
# E28B/T4 (D-B3) — `image_digest` on the body, and TRUNK-GATED candidate registration
# --------------------------------------------------------------------------- #
#
# Two separate defects close here.
#
# 1. `RuntimeBuildRequest` had no `image_digest` field. The template workflow already POSTed one and
#    pydantic ignores unknown keys, so the digest was accepted with a 202 and DISCARDED — no error
#    on either side while the whole digest contract was a no-op end to end.
#
# 2. E28B deleted the workflow's `candidate` job, which was the ONLY caller of
#    `/builds/prod-candidate`. That left NOTHING writing the candidate block, so `promote_repo`
#    refused every promotion with `no_prod_candidate` permanently. This route is now the registrar:
#    it already proves, from the same validated OIDC token, which artifact, who merged and which
#    commit.
#
# The TRUNK GATE is the governance control. A live probe found a bare template tree on `main`
# arriving `prod_candidate_status: pending` — promotable to production with no human-reviewed
# content. Registering on every build would reopen that from any branch, so only the owning
# project's own trunk (D-B5 — a project setting, never a `main` literal) may produce a candidate.

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
        conn_svc=conn_svc, build_svc=build_svc,
        claims=claims if claims is not None else _claims(), proj_svc=proj_svc,
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
        "sha256:" + "a" * 63,           # truncated
        "sha256:" + "a" * 65,           # too long
        "sha256:" + "AB" * 32,          # uppercase — registries emit lowercase
        "ab" * 32,                      # no algorithm prefix
        "sha512:" + "a" * 64,           # wrong algorithm
        "None",                         # what the AWS CLI prints for a missing image
        _GOOD_DIGEST + "x",             # trailing junk
        _GOOD_DIGEST + "; echo hi",     # a shell metacharacter, bound for a docker reference
    ],
    ids=["truncated", "too-long", "uppercase", "no-prefix", "wrong-algo", "literal-None",
         "trailing-junk", "metachar"],
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
        sha="abc",              # from the TOKEN
        actor="merging-human",  # from the TOKEN
    )


@pytest.mark.parametrize(
    "trunk,ref",
    [
        ("main", "refs/heads/feature/x"),
        ("main", "refs/heads/dev"),
        ("release", "refs/heads/main"),   # `main` is NOT the trunk here
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
    build_svc.start_runtime_build.assert_called_once()  # the dev deploy is unaffected
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
    # A spec listing ONLY `connection_id` — the attribute the binding check needs — so
    # `getattr(..., "trunk_branch", None)` genuinely misses. A plain MagicMock would auto-create
    # `trunk_branch` and return a truthy mock, making this test pass for the wrong reason (it would
    # compare the ref against a mock's repr and never exercise the fallback at all).
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
    resp, proj_svc, build_svc = _post(
        {"image_tag": "agent-OTHER-abc", "image_digest": _GOOD_DIGEST}
    )
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
        # `assert_not_called()` RAISES on failure, so it needs no `assert` and must not be written
        # as `... , label` — a bare expression with a comma is a tuple that evaluates and discards.
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
    behind. A prod-stage POST is refused over OIDC (E27/T9) — it must not register either."""
    resp, proj_svc, build_svc = _post(
        {"stage": "prod", "image_digest": _GOOD_DIGEST}
    )
    assert resp.status_code == 403
    build_svc.start_runtime_build.assert_not_called()
    proj_svc.record_prod_candidate.assert_not_called()
