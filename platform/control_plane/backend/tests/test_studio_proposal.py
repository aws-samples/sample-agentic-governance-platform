"""The Studio proposal service: validate, (secret), branch, commit, PR, declare.

Every collaborator is a fake, so the tests pin the ORDER and the WRITES: a refused request
writes nothing, files land only on ``studio/<repo_id[:8]>``, and the API key reaches only the
secret store."""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest
import yaml

from models.repository import PullRequestView
from models.studio import AgentProposalRequest, McpProposalRequest, ProposalResponse
from services.eval_config import parse_dataset
from services.github_pr_service import GitHubPrDuplicateError, GitHubPrError
from services.github_user_link import GitHubLinkError
from services.repo_provider import RepoConflictError, RepoView
from services.studio_proposal import (
    _REQUEST_KEYS,
    DEFAULT_EVAL_ITEMS,
    StudioConflictError,
    StudioProposalService,
    StudioProviderError,
    StudioValidationError,
)

REPO_ID = "abcd1234ef567890"
BRANCH = "studio/abcd1234"
KEY = "sk-live-NEVER-ANYWHERE-42"
SECRET_ARN = "arn:aws:secretsmanager:us-east-1:000000000000:secret:agp-mcp/claims-api/api-key-AbCdEf"

SPEC = {
    "openapi": "3.0.3",
    "info": {"title": "Claims", "version": "1"},
    "servers": [{"url": "https://claims.example.org"}],
    "paths": {
        "/policies/{id}": {
            "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}],
            "get": {
                "operationId": "getPolicy",
                "responses": {
                    "200": {
                        "description": "ok",
                        "content": {"application/json": {"schema": {"type": "object"}}},
                    }
                },
            },
        },
        "/claims": {
            "post": {
                "operationId": "openClaim",
                "requestBody": {"content": {"application/json": {"schema": {"type": "object"}}}},
                "responses": {"201": {"description": "ok"}},
            },
            "get": {"operationId": "listClaims", "responses": {"200": {"description": "ok"}}},
        },
    },
}


def _spec(**changes) -> str:
    import copy

    spec = copy.deepcopy(SPEC)
    for k, v in changes.items():
        spec[k] = v
    return yaml.safe_dump(spec, sort_keys=False)


def _skill(name="claims-triage", desc="Triage a claim", fm_name=None) -> dict:
    return {
        "name": name,
        "content": f"---\nname: {fm_name or name}\ndescription: {desc}\n---\n# Body\nDo it.\n",
    }


USER_TOKEN = "ghu_linked_user_token"
LOGIN = "lars-svensson"


class FakeRepos:
    """A tiny git: ``branches`` name shas; ``meta`` holds each commit's tree and parent. Overlay
    commits only, like the real provider. ``hidden_from`` = tokens whose ``read_repo`` sees nothing.
    ``prs`` is the fixture's FakePrs, so deleting a PR's head branch closes it, as on GitHub."""

    def __init__(self, *, commit_conflict=False):
        self.calls = []
        self.commit_conflict = commit_conflict
        self.branches = {"trunk": "tip-sha"}
        self.meta = {"tip-sha": {"tree": {"README.md": b"template"}, "parent": None}}
        self.hidden_from = set()
        self.commits = 0
        self.prs = None

    @property
    def committed(self):
        """What Studio wrote: the studio branch's tree minus the template's files."""
        tree = self.meta[self.branches[BRANCH]]["tree"] if BRANCH in self.branches else {}
        return {p: c for p, c in tree.items() if p != "README.md"} or None

    def push(self, branch, files):
        """Someone else's commit on ``branch``."""
        parent = self.branches[branch]
        self.commits += 1
        sha = f"commit-{self.commits}"
        self.meta[sha] = {"tree": {**self.meta[parent]["tree"], **files}, "parent": parent}
        self.branches[branch] = sha
        return sha

    def read_repo(self, org, repo, *, token, base_url=None):
        self.calls.append(("read_repo", org, repo, token))
        if token in self.hidden_from:
            return None
        return RepoView(default_branch="trunk", head_sha=self.branches["trunk"])

    def create_branch(self, org, repo, *, branch, from_branch, token, base_url=None):
        self.calls.append(("create_branch", branch, from_branch))
        tip = self.branches[from_branch]
        current = self.branches.setdefault(branch, tip)
        if current != tip:
            raise RepoConflictError("exists elsewhere")
        return tip

    def delete_branch(self, org, repo, branch, *, token, base_url=None):
        self.calls.append(("delete_branch", branch))
        self.branches.pop(branch, None)
        for pr in self.prs.open if self.prs else []:
            if pr.head_ref == branch:
                pr.state = "closed"

    def commit_files_on_head(
        self, org, repo, files, *, branch, expected_head_sha, message, token, base_url=None
    ):
        self.calls.append(("commit", branch, expected_head_sha, message))
        if self.commit_conflict or self.branches[branch] != expected_head_sha:
            raise RepoConflictError("moved")
        if {**self.meta[expected_head_sha]["tree"], **files} == self.meta[expected_head_sha]["tree"]:
            return expected_head_sha
        return self.push(branch, files)


class FakePrs:
    """GitHub's rule: one open PR per head branch; the head sha is read live off the branch."""

    def __init__(self, repos, error=None):
        self.calls = []
        self.error = error
        self.open = []
        self.repos = repos

    def _view(self, pr):
        return PullRequestView(
            number=pr.number,
            title="t",
            state=pr.state,
            author=LOGIN,
            head_sha=self.repos.branches.get(pr.head_ref, ""),
            url=f"https://github.example/acme/r/pull/{pr.number}",
            can_approve=False,
            approve_blocked_reason="x",
            mergeable=None,
        )

    def create_pull_request(self, org, repo, token, *, viewer_login, title, head, base, body, base_url=None):
        self.calls.append(
            {
                "token": token,
                "viewer_login": viewer_login,
                "title": title,
                "head": head,
                "base": base,
                "body": body,
            }
        )
        if self.error:
            raise self.error
        if any(p.state == "open" and p.head_ref == head for p in self.open):
            raise GitHubPrDuplicateError("exists", kind="conflict")
        return self._view(self.add(head))

    def add(self, head):
        pr = SimpleNamespace(number=7 + len(self.open), state="open", head_ref=head)
        self.open.append(pr)
        return pr


class FakeLinks:
    """The GitHub user-link service: one linked human on connection c1."""

    def __init__(self):
        self.linked = True

    def get_user_bearer_token(self, oid, connection_id):
        if not self.linked or oid != "oid-lars" or connection_id != "c1":
            raise GitHubLinkError("no link", kind="not_found")
        return USER_TOKEN

    def list_for_principal(self, oid):
        return (
            [
                SimpleNamespace(connection_id="c9", github_login="someone-else"),
                SimpleNamespace(connection_id="c1", github_login=LOGIN),
            ]
            if self.linked
            else []
        )


class FakeSecrets:
    class exceptions:
        class ResourceExistsException(Exception):
            pass

    def __init__(self, exists=False):
        self.calls = []
        self.exists = exists

    def create_secret(self, *, Name, SecretString, **kw):
        self.calls.append(("create", Name, SecretString))
        if self.exists:
            raise self.exceptions.ResourceExistsException("taken")
        return {"ARN": SECRET_ARN}

    def put_secret_value(self, *, SecretId, SecretString):
        self.calls.append(("put", SecretId, SecretString))
        return {"ARN": SECRET_ARN}


class FakeRegistry:
    def __init__(self):
        self.agent = SimpleNamespace(id="agent-1", name="claims_agent", declared_mcp_server_ids=[])
        self.persisted = []

    def get(self, agent_id):
        return self.agent if agent_id == "agent-1" else None

    def persist_identity(self, agent):
        self.persisted.append(list(agent.declared_mcp_server_ids))
        return agent


class FakeMcp:
    def __init__(self):
        self.records = {
            "mcp-1": SimpleNamespace(id="mcp-1", name="claims-api", description="Claims API tools"),
            "mcp-2": SimpleNamespace(id="mcp-2", name="fnol", description="FNOL"),
        }

    def get(self, mcp_id):
        return self.records.get(mcp_id)


class FakeConn:
    def get_connection(self, connection_id):
        return SimpleNamespace(org="acme", base_url=None)

    def get_bearer_token(self, connection_id):
        return "ghs_app_token"


@pytest.fixture
def env():
    repos = FakeRepos()
    e = SimpleNamespace(
        repos=repos,
        prs=FakePrs(repos),
        secrets=FakeSecrets(),
        registry=FakeRegistry(),
        mcp=FakeMcp(),
        rebuilt=[],
        links=FakeLinks(),
        caller=SimpleNamespace(oid="oid-lars", email="lars@example.org"),
    )
    repos.prs = e.prs

    def svc(**kw):
        real = StudioProposalService(
            repos=e.repos,
            connections=FakeConn(),
            prs=e.prs,
            registry=e.registry,
            mcp_registry=e.mcp,
            secrets=e.secrets,
            links=e.links,
            rebuild_env=kw.pop("rebuild_env", lambda agent: e.rebuilt.append(agent.id)),
        )
        return SimpleNamespace(
            propose=lambda p, r, q, caller=None: real.propose(p, r, q, caller=caller or e.caller)
        )

    e.svc = svc
    e.project = SimpleNamespace(id="p1", connection_id="c1", trunk_branch="trunk")
    e.mcp_repo = SimpleNamespace(
        id=REPO_ID,
        name="claims-api",
        status="ready",
        artifact_kind="mcp_server",
        mcp_server_id="mcp-1",
        agent_id=None,
    )
    e.agent_repo = SimpleNamespace(
        id=REPO_ID,
        name="claims_agent",
        status="ready",
        artifact_kind="agent",
        mcp_server_id=None,
        agent_id="agent-1",
    )
    return e


def _mcp_req(**kw):
    req = {
        "kind": "mcp_server",
        "openapi_text": _spec(),
        "exposed_operations": ["getPolicy", "openClaim"],
        "auth": "none",
    }
    req.update(kw)
    return req


def _agent_req(**kw):
    req = {
        "kind": "agent",
        "system_prompt": "You triage claims.",
        "skills": [_skill()],
        "declared_mcp_server_ids": ["mcp-1"],
    }
    req.update(kw)
    return req


def _nothing_written(e):
    assert e.repos.calls == [] and e.prs.calls == [] and e.secrets.calls == [] and e.registry.persisted == []


_BAD_PATHS = {
    "/x": {"get": {"responses": {"200": {"description": "ok"}}}},
}


@pytest.mark.parametrize(
    "req",
    [
        _mcp_req(openapi_text=_spec(paths=_BAD_PATHS)),
        _mcp_req(openapi_text=_spec(components={"schemas": {"A": {"oneOf": [{"type": "string"}]}}})),
        _mcp_req(
            openapi_text=_spec(
                paths={
                    "/x": {
                        "get": {
                            "operationId": "getPolicy",
                            "responses": {
                                "200": {
                                    "description": "ok",
                                    "content": {"application/json": {"schema": {"anyOf": []}}},
                                }
                            },
                        }
                    }
                }
            )
        ),
        _mcp_req(openapi_text=_spec(components={"schemas": {"A": {"allOf": []}}})),
        _mcp_req(openapi_text=_spec(security=[{"k": []}])),
        _mcp_req(openapi_text=_spec(components={"securitySchemes": {"k": {"type": "apiKey"}}})),
        _mcp_req(
            openapi_text=_spec(
                paths={
                    "/x": {
                        "post": {
                            "operationId": "getPolicy",
                            "requestBody": {"content": {"text/plain": {}}},
                            "responses": {"200": {"description": "ok"}},
                        }
                    }
                }
            )
        ),
        _mcp_req(openapi_text=_spec(servers=[{"url": ""}])),
        _mcp_req(openapi_text=_spec(servers=[])),
        _mcp_req(openapi_text=_spec(servers=[{"url": "https://REPLACE-ME.example.com"}])),
        _mcp_req(openapi_text="x" * (1024 * 1024 + 1)),
        _mcp_req(openapi_text="paths: [unclosed"),
        _mcp_req(exposed_operations=[]),
        _mcp_req(exposed_operations=["getPolicy", "nope"]),
        _mcp_req(auth="api_key"),
        _mcp_req(auth="oauth"),
        _mcp_req(
            auth="api_key",
            existing_secret_arn="arn:aws:secretsmanager:us-east-1:000000000000:secret:agp-mcp/other/api-key-AbCdEf",
        ),
        _mcp_req(
            auth="api_key",
            existing_secret_arn="arn:aws:secretsmanager:us-east-1:000000000000:secret:platform/root-AbCdEf",
        ),
        _mcp_req(unexpected="x"),
    ],
    ids=[
        "no-operationId",
        "oneOf",
        "anyOf",
        "allOf",
        "security",
        "securitySchemes",
        "media-type",
        "empty-server",
        "no-server",
        "placeholder-server",
        "too-large",
        "bad-yaml",
        "no-exposed",
        "unknown-op",
        "api-key-missing",
        "bad-auth",
        "secret-other-mcp",
        "secret-outside-namespace",
        "unknown-field",
    ],
)
def test_mcp_validation_refuses_and_writes_nothing(env, req):
    with pytest.raises(StudioValidationError):
        env.svc().propose(env.project, env.mcp_repo, req)
    _nothing_written(env)


@pytest.mark.parametrize(
    "req",
    [
        _agent_req(skills=[{"name": "triage", "content": "# no frontmatter\n"}]),
        _agent_req(skills=[{"name": "triage", "content": "---\nname: triage\n---\nbody\n"}]),
        _agent_req(skills=[{"name": "triage", "content": "---\ndescription: d\n---\nbody\n"}]),
        _agent_req(skills=[_skill(name="Triage_Claims")]),
        _agent_req(skills=[_skill(name="triage", fm_name="other")]),
        _agent_req(skills=[_skill(), _skill()]),
        _agent_req(skills=[{"name": "../x", "content": "---\nname: ../x\ndescription: d\n---\n"}]),
        _agent_req(declared_mcp_server_ids=["mcp-1", "mcp-unknown"]),
        _agent_req(system_prompt="", skills=[]),
        _agent_req(system_prompt="x" * (1024 * 1024 + 1)),
        _agent_req(extra="x"),
    ],
    ids=[
        "no-frontmatter",
        "no-description",
        "no-name",
        "bad-name",
        "name-not-folder",
        "duplicate",
        "traversal",
        "unknown-mcp",
        "nothing",
        "prompt-too-large",
        "unknown-field",
    ],
)
def test_agent_validation_refuses_and_writes_nothing(env, req):
    with pytest.raises(StudioValidationError):
        env.svc().propose(env.project, env.agent_repo, req)
    _nothing_written(env)


def test_repo_not_ready_is_a_conflict(env):
    env.mcp_repo.status = "provisioning"
    with pytest.raises(StudioConflictError):
        env.svc().propose(env.project, env.mcp_repo, _mcp_req())
    _nothing_written(env)


def test_kind_mismatch_is_a_conflict(env):
    with pytest.raises(StudioConflictError):
        env.svc().propose(env.project, env.mcp_repo, _agent_req())
    _nothing_written(env)


def test_a_moved_branch_on_commit_is_a_conflict(env):
    env.repos.commit_conflict = True
    with pytest.raises(StudioConflictError):
        env.svc().propose(env.project, env.mcp_repo, _mcp_req())
    assert env.prs.calls == []


def test_a_provider_failure_is_a_provider_error(env):
    env.prs.error = GitHubPrError("boom", kind="provider_error")
    with pytest.raises(StudioProviderError):
        env.svc().propose(env.project, env.agent_repo, _agent_req())
    assert env.registry.persisted == []


def test_mcp_auth_none_commits_exactly_the_two_files_on_the_studio_branch(env):
    out = env.svc().propose(env.project, env.mcp_repo, _mcp_req())
    assert out == {
        "branch": BRANCH,
        "pr_number": 7,
        "pr_url": "https://github.example/acme/r/pull/7",
        "files": ["gateway.yaml", "openapi.yaml"],
    }
    assert set(env.repos.committed) == {"openapi.yaml", "gateway.yaml"}
    assert ("create_branch", BRANCH, "trunk") in env.repos.calls
    commit = next(c for c in env.repos.calls if c[0] == "commit")
    assert commit[1] == BRANCH and commit[2] == "tip-sha"
    assert env.repos.branches["trunk"] == "tip-sha"
    assert env.prs.calls[0]["head"] == BRANCH and env.prs.calls[0]["base"] == "trunk"
    assert env.prs.calls[0]["token"] == USER_TOKEN and env.prs.calls[0]["viewer_login"] == LOGIN
    assert env.prs.calls[0]["title"] == "Studio: claims-api"
    gw = yaml.safe_load(env.repos.committed["gateway.yaml"])
    assert gw["auth"] == "none" and "api_key" not in gw
    assert gw["openapi_spec"] == "openapi.yaml" and gw["description"] == "Claims API tools"
    assert env.secrets.calls == []


def test_the_request_models_name_exactly_the_keys_the_service_accepts():
    assert set(McpProposalRequest.model_fields) == _REQUEST_KEYS["mcp_server"]
    assert set(AgentProposalRequest.model_fields) == _REQUEST_KEYS["agent"]


def test_the_201_body_validates_through_proposal_response(env):
    out = env.svc().propose(env.project, env.mcp_repo, _mcp_req())
    assert set(ProposalResponse.model_fields) == set(out)
    assert ProposalResponse.model_validate(out).model_dump(mode="json") == out


def test_openapi_is_pruned_to_the_exposed_operations(env):
    env.svc().propose(env.project, env.mcp_repo, _mcp_req(exposed_operations=["openClaim"]))
    spec = yaml.safe_load(env.repos.committed["openapi.yaml"])
    assert list(spec["paths"]) == ["/claims"]
    assert list(spec["paths"]["/claims"]) == ["post"]
    assert spec["servers"] == SPEC["servers"]


def test_mcp_api_key_creates_the_secret_and_the_key_appears_nowhere_else(env, caplog):
    caplog.set_level(logging.DEBUG)
    out = env.svc().propose(env.project, env.mcp_repo, _mcp_req(auth="api_key", api_key=KEY))
    assert env.secrets.calls[0][:2] == ("create", "agp-mcp/claims-api/api-key")
    assert KEY in env.secrets.calls[0][2]
    gw = yaml.safe_load(env.repos.committed["gateway.yaml"])
    assert gw["auth"] == "api_key" and gw["api_key"]["secret_arn"] == SECRET_ARN
    assert gw["api_key"]["json_key"] == "api_key"
    for content in env.repos.committed.values():
        assert KEY.encode() not in content
    assert KEY not in repr(out) and KEY not in repr(env.prs.calls) and KEY not in repr(env.repos.calls)
    assert all(KEY not in r.getMessage() for r in caplog.records)


def test_mcp_api_key_reuses_an_existing_secret_of_the_same_name(env):
    env.secrets.exists = True
    env.svc().propose(env.project, env.mcp_repo, _mcp_req(auth="api_key", api_key=KEY))
    assert [c[0] for c in env.secrets.calls] == ["create", "put"]
    assert env.secrets.calls[1][1] == "agp-mcp/claims-api/api-key"
    assert yaml.safe_load(env.repos.committed["gateway.yaml"])["api_key"]["secret_arn"] == SECRET_ARN


def test_mcp_existing_secret_arn_is_used_without_creating_one(env):
    env.svc().propose(env.project, env.mcp_repo, _mcp_req(auth="api_key", existing_secret_arn=SECRET_ARN))
    assert env.secrets.calls == []
    assert yaml.safe_load(env.repos.committed["gateway.yaml"])["api_key"]["secret_arn"] == SECRET_ARN


def test_agent_commits_prompt_and_skills_then_declares_after_the_pr(env):
    out = env.svc().propose(
        env.project, env.agent_repo, _agent_req(declared_mcp_server_ids=["mcp-1", "mcp-2", "mcp-1"])
    )
    assert out["files"] == ["evals/dataset.jsonl", "prompt/system.md", "skills/claims-triage/SKILL.md"]
    assert set(env.repos.committed) == {
        "evals/dataset.jsonl",
        "prompt/system.md",
        "skills/claims-triage/SKILL.md",
    }
    assert env.repos.committed["prompt/system.md"] == b"You triage claims.\n"
    assert env.repos.committed["skills/claims-triage/SKILL.md"] == _skill()["content"].encode()
    assert env.prs.calls[0]["title"] == "Studio: claims_agent"
    assert env.registry.persisted == [["mcp-1", "mcp-2"]]


def test_agent_without_a_prompt_writes_only_skills(env):
    env.svc().propose(env.project, env.agent_repo, _agent_req(system_prompt="  "))
    assert set(env.repos.committed) == {"evals/dataset.jsonl", "skills/claims-triage/SKILL.md"}


def test_never_commits_to_the_default_branch(env):
    env.svc().propose(env.project, env.agent_repo, _agent_req())
    assert all(c[1] == BRANCH for c in env.repos.calls if c[0] == "commit")


def test_a_yaml_alias_bomb_is_walked_in_linear_time(env):
    """Aliases share nodes; ten levels of ten aliases is 10^10 nodes to a naive walk."""
    levels = ["a0: &a0 {type: string}"]
    levels += [f"a{i}: &a{i} [{', '.join([f'*a{i - 1}'] * 10)}]" for i in range(1, 11)]
    text = _spec() + "x-bomb:\n" + "\n".join(f"  {line}" for line in levels) + "\n"
    out = env.svc().propose(env.project, env.mcp_repo, _mcp_req(openapi_text=text))
    assert out["files"] == ["gateway.yaml", "openapi.yaml"]


def test_a_failed_declare_after_the_pr_is_a_provider_error_naming_the_pr(env):
    def boom(agent):
        raise RuntimeError("registry down")

    env.registry.persist_identity = boom
    with pytest.raises(StudioProviderError, match="#7"):
        env.svc().propose(env.project, env.agent_repo, _agent_req())


def test_retry_after_a_pr_failure_succeeds_with_a_new_pr(env):
    env.prs.error = GitHubPrError("github down", kind="provider_error")
    with pytest.raises(StudioProviderError):
        env.svc().propose(env.project, env.agent_repo, _agent_req())
    assert env.registry.persisted == []
    env.prs.error = None
    out = env.svc().propose(env.project, env.agent_repo, _agent_req())
    assert out["pr_number"] == 7 and env.registry.persisted == [["mcp-1"]]
    assert env.repos.meta[env.repos.branches[BRANCH]]["parent"] == "tip-sha"


def test_retry_after_a_declare_failure_recuts_reopens_and_declares(env):
    real = env.registry.persist_identity
    env.registry.persist_identity = lambda agent: (_ for _ in ()).throw(RuntimeError("down"))
    with pytest.raises(StudioProviderError):
        env.svc().propose(env.project, env.agent_repo, _agent_req())
    env.registry.agent.declared_mcp_server_ids = []
    env.registry.persist_identity = real
    out = env.svc().propose(env.project, env.agent_repo, _agent_req())
    assert out["pr_number"] == 8 and [p.state for p in env.prs.open] == ["closed", "open"]
    assert env.registry.persisted == [["mcp-1"]]


def test_a_pr_conflict_after_the_recut_says_try_again(env):
    env.prs.error = GitHubPrDuplicateError("exists", kind="conflict")
    with pytest.raises(StudioConflictError, match="still closing the previous pull request"):
        env.svc().propose(env.project, env.agent_repo, _agent_req())


def test_an_unchanged_tree_with_no_pr_is_a_validation_error(env):
    env.prs.error = GitHubPrError("no commits", kind="no_commits")
    with pytest.raises(StudioValidationError, match="Nothing changed"):
        env.svc().propose(env.project, env.agent_repo, _agent_req())


@pytest.mark.parametrize(
    "on_repo, field",
    [
        (True, "gateway_arn"),
        (True, "gateway_id"),
        (False, "gateway_arn"),
        (False, "gateway_url"),
    ],
)
def test_a_deployed_mcp_server_is_refused(env, on_repo, field):
    setattr(env.mcp_repo if on_repo else env.mcp.records["mcp-1"], field, "x")
    with pytest.raises(StudioConflictError, match="already been deployed"):
        env.svc().propose(env.project, env.mcp_repo, _mcp_req(auth="api_key", api_key=KEY))
    _nothing_written(env)


@pytest.mark.parametrize(
    "field",
    [
        "last_dev_image_tag",
        "last_dev_digest",
        "last_build_started_at",
        "last_promoted_image_tag",
        "runtime_arns",
    ],
)
def test_a_deployed_agent_is_refused(env, field):
    if field == "runtime_arns":
        env.registry.agent.agent_arns = {"dev": "arn:aws:bedrock-agentcore:x"}
    else:
        setattr(env.agent_repo, field, "x")
    with pytest.raises(StudioConflictError, match="already been deployed"):
        env.svc().propose(env.project, env.agent_repo, _agent_req())
    _nothing_written(env)


def test_env_rebuild_runs_only_when_the_declared_list_changed(env):
    env.svc().propose(env.project, env.agent_repo, _agent_req())
    assert env.rebuilt == ["agent-1"]
    env.svc().propose(env.project, env.agent_repo, _agent_req())
    assert env.rebuilt == ["agent-1"]


def test_a_failed_env_rebuild_is_swallowed(env):
    def boom(agent):
        raise RuntimeError("runtime api down")

    svc = env.svc(rebuild_env=boom)
    assert svc.propose(env.project, env.agent_repo, _agent_req())["pr_number"] == 7


def _real_agent(**kw):
    from datetime import UTC, datetime

    from models.agent import Agent, LifecycleState

    now = datetime(2026, 10, 13, tzinfo=UTC)
    return Agent(
        id="agent-1",
        name="claims_agent",
        lifecycle_state=LifecycleState.PROPOSED,
        created_at=now,
        updated_at=now,
        **kw,
    )


def test_a_real_agent_without_runtimes_is_proposed(env):
    env.registry.agent = _real_agent()
    assert env.svc().propose(env.project, env.agent_repo, _agent_req())["pr_number"] == 7
    assert env.registry.agent.declared_mcp_server_ids == ["mcp-1"]


@pytest.mark.parametrize(
    "kw",
    [{"agent_arns": {"dev": "arn:aws:bedrock-agentcore:x"}}, {"agent_arn": "arn:aws:bedrock-agentcore:x"}],
)
def test_a_real_agent_with_a_runtime_is_refused(env, kw):
    env.registry.agent = _real_agent(**kw)
    with pytest.raises(StudioConflictError, match="already been deployed"):
        env.svc().propose(env.project, env.agent_repo, _agent_req())
    _nothing_written(env)


def test_a_retry_with_fewer_skills_recuts_the_branch_so_the_dropped_one_is_gone(env):
    first = env.svc().propose(
        env.project, env.agent_repo, _agent_req(skills=[_skill("skill-a"), _skill("skill-b")])
    )
    second = env.svc().propose(env.project, env.agent_repo, _agent_req(skills=[_skill("skill-a")]))
    assert set(env.repos.committed) == {"evals/dataset.jsonl", "prompt/system.md", "skills/skill-a/SKILL.md"}
    assert ("delete_branch", BRANCH) in env.repos.calls
    assert env.repos.meta[env.repos.branches[BRANCH]]["parent"] == "tip-sha"
    assert second["pr_number"] != first["pr_number"]
    assert [p.state for p in env.prs.open] == ["closed", "open"]


def test_a_non_studio_commit_on_the_branch_is_discarded(env):
    env.repos.branches[BRANCH] = env.repos.branches["trunk"]
    env.repos.push(BRANCH, {".github/workflows/build.yml": b"evil"})
    env.prs.add(BRANCH)
    out = env.svc().propose(env.project, env.agent_repo, _agent_req())
    assert ".github/workflows/build.yml" not in env.repos.meta[env.repos.branches[BRANCH]]["tree"]
    assert env.repos.meta[env.repos.branches[BRANCH]]["parent"] == "tip-sha"
    assert [p.state for p in env.prs.open] == ["closed", "open"] and out["pr_number"] == 8


def test_any_other_pr_conflict_is_a_neutral_provider_error(env):
    env.prs.error = GitHubPrError("head invalid", kind="conflict")
    with pytest.raises(StudioProviderError) as exc:
        env.svc().propose(env.project, env.agent_repo, _agent_req())
    assert "closing" not in str(exc.value)


def _no_writes(e):
    assert not any(c[0] in ("create_branch", "delete_branch", "commit") for c in e.repos.calls)
    assert e.prs.calls == [] and e.secrets.calls == [] and e.registry.persisted == []


def test_the_pr_is_opened_with_the_linked_token_and_login_and_the_branch_with_the_app_token(env, caplog):
    caplog.set_level(logging.DEBUG)
    env.svc().propose(env.project, env.mcp_repo, _mcp_req(auth="api_key", api_key=KEY))
    pr = env.prs.calls[0]
    assert pr["token"] == USER_TOKEN and pr["viewer_login"] == LOGIN
    reads = [c for c in env.repos.calls if c[0] == "read_repo"]
    assert reads[0][3] == USER_TOKEN and reads[1][3] == "ghs_app_token"
    assert all(USER_TOKEN not in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("break_it", ["unlinked", "no_oid"])
def test_no_linked_account_is_a_409_and_nothing_is_written(env, break_it):
    if break_it == "unlinked":
        env.links.linked = False
    else:
        env.caller = SimpleNamespace(oid=None, email="dev@example.org")
    with pytest.raises(StudioConflictError, match="connect your GitHub account to act on pull requests"):
        env.svc().propose(env.project, env.mcp_repo, _mcp_req(auth="api_key", api_key=KEY))
    _nothing_written(env)


def test_a_linked_account_that_cannot_see_the_repo_is_a_409_and_nothing_is_written(env):
    env.repos.hidden_from.add(USER_TOKEN)
    with pytest.raises(StudioConflictError, match="cannot open pull requests on this repository"):
        env.svc().propose(env.project, env.agent_repo, _agent_req())
    _no_writes(env)


@pytest.mark.parametrize("kind", ["capability_missing", "not_found"])
def test_a_pr_refused_for_the_linked_account_is_a_409(env, kind):
    env.prs.error = GitHubPrError("no", kind=kind)
    with pytest.raises(StudioConflictError, match="cannot open pull requests on this repository"):
        env.svc().propose(env.project, env.agent_repo, _agent_req())


HAPPY = {
    "id": "claims_agent-1",
    "input": "I had a rear-end collision; policy POL-1002.",
    "assertions": ["Opens a claim"],
    "expected_tools": [{"name": "getPolicy"}, {"name": "openClaim"}],
}
REFUSE = {"id": "claims_agent-2", "input": "What is the weather?", "expected_tools": []}


def test_agent_commits_its_eval_items_as_the_e42_dataset(env):
    out = env.svc().propose(env.project, env.agent_repo, _agent_req(eval_items=[HAPPY, REFUSE]))
    assert "evals/dataset.jsonl" in out["files"]
    items = parse_dataset(env.repos.committed["evals/dataset.jsonl"].decode())
    assert [i.id for i in items] == ["claims_agent-1", "claims_agent-2"]
    assert [t.name for t in items[0].expected_tools] == ["getPolicy", "openClaim"]


def test_agent_without_eval_items_gets_the_default_refusal_item(env):
    env.svc().propose(env.project, env.agent_repo, _agent_req())
    lines = env.repos.committed["evals/dataset.jsonl"].decode().splitlines()
    assert [json.loads(x)["id"] for x in lines] == [d["id"] for d in DEFAULT_EVAL_ITEMS]


@pytest.mark.parametrize(
    "items,match",
    [
        ("nope", "eval_items must be a list"),
        ([{"id": "a"}], "An eval item is invalid"),
        ([{"id": "a", "input": "x", "typo": 1}], "An eval item is invalid"),
        ([{"id": "a", "input": "x"}, {"id": "a", "input": "y"}], "duplicate id"),
    ],
)
def test_invalid_eval_items_are_refused_before_any_write(env, items, match):
    with pytest.raises(StudioValidationError, match=match):
        env.svc().propose(env.project, env.agent_repo, _agent_req(eval_items=items))
    _nothing_written(env)
