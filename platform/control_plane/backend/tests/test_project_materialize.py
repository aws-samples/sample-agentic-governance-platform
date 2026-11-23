"""Service tests for adding and materializing a repository: ``add_repo`` and its refusals, the materialize
steps over the provider seam and their retry, the template sources, and the repository's CI variables.

Every collaborator is faked (``MagicMock``, ``SimpleNamespace`` or a small class here); no live AWS,
Entra or GitHub. ``_FakeGit`` models per-branch trees with content-addressed tree shas, which is what
lets "the pushed tree is exactly the scaffold" and "a re-push writes no second commit" fail at all.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from conftest import (
    _SCAFFOLD_FILES,
    FIXED,
    TEMPLATE_NAME,
    VALID_AGENT_CONFIG,
    _make_project,
    _principal,
)
from seed import seed

from models.agent import DataClassification
from models.repository import Repository, StepStatus
from models.tenant import TenantStageConfig
from services import project_materialize
from services.github_repo_errors import GitHubRepoError
from services.github_repo_service import GitHubRepoService
from services.infra_template_registry import SUPPORTED_BUILD_KINDS
from services.project_errors import ProjectError
from services.project_materialize import (
    _CONFIG_BUILD_KIND,
    _CONTAINER_BUILD_KIND,
    _STAGE_SCOPED_VAR_KEYS,
    _build_stage_ci_vars,
)
from services.project_service import _OPS_MCP_NAME_RE, ProjectService
from services.project_store import _PROJECT_PK, _to_item
from services.repo_provider import RepoView
from services.template_registry import TemplateRecord


def test_add_repo_creates_the_repo_and_pushes_the_template(project_service_fakes):
    """The repo is CREATED EMPTY and AGP pushes the template itself."""
    svc = project_service_fakes
    _make_project(svc)

    _add_and_materialize(
        svc,
        project_id="proj-1",
        name="my-agent",
        template_name=TEMPLATE_NAME,
        agent_config=VALID_AGENT_CONFIG,
        created_by="op@x",
        principal=_principal(),
    )

    svc._rollout.create_repo.assert_called_once()
    assert svc._rollout.create_repo.call_args.args[:2] == ("acme", "my-agent")
    assert svc._rollout.create_repo.call_args.kwargs["private"] is True
    svc._rollout.commit_files.assert_called_once()
    call = svc._rollout.commit_files.call_args
    assert call.args[:2] == ("acme", "my-agent")
    assert call.args[2] == _EXPECTED_PUSHED_TREE
    assert call.kwargs["branch"] == "main"
    assert call.kwargs["message"] == "chore: initialize from template [skip ci]"


def test_add_repo_sets_sponsor_from_principal(project_service_fakes):
    svc = project_service_fakes
    _make_project(svc)

    svc.add_repo(
        project_id="proj-1",
        name="my-agent",
        template_name=TEMPLATE_NAME,
        agent_config=VALID_AGENT_CONFIG,
        created_by="op@x",
        principal=_principal(oid="O1", email="e@x"),
    )

    created = svc._registry.create.call_args.args[0]
    assert created.sponsor_oid == "O1"
    assert created.sponsor_email == "e@x"


def test_add_repo_passes_governance_fields(project_service_fakes):
    svc = project_service_fakes
    _make_project(svc)

    svc.add_repo(
        project_id="proj-1",
        name="my-agent",
        template_name=TEMPLATE_NAME,
        agent_config=VALID_AGENT_CONFIG,
        created_by="op@x",
        principal=_principal(),
        business_unit="Markets",
        region="emea",
        data_classification="Confidential",
    )

    created = svc._registry.create.call_args.args[0]
    assert created.business_unit == "Markets"
    assert created.region == "emea"
    assert created.data_classification == DataClassification.CONFIDENTIAL


def test_add_repo_has_no_enrollment_gate(project_service_fakes):
    svc = project_service_fakes
    _make_project(svc)

    assert not hasattr(svc, "_enrollment")

    repo = _add_and_materialize(
        svc,
        project_id="proj-1",
        name="my-agent",
        template_name=TEMPLATE_NAME,
        agent_config=VALID_AGENT_CONFIG,
        created_by="op@x",
        principal=_principal(),
    )
    assert repo.status == "ready"
    svc._rollout.create_repo.assert_called_once()


def test_add_repo_layers_ci_vars(project_service_fakes):
    svc = project_service_fakes
    _make_project(svc)

    _add_and_materialize(
        svc,
        project_id="proj-1",
        name="my-agent",
        template_name=TEMPLATE_NAME,
        agent_config=VALID_AGENT_CONFIG,
        created_by="op@x",
        principal=_principal(),
        repo_overrides={"ACCOUNT_ID": "123"},
    )

    set_vars = svc._rollout.set_ci_vars.call_args.args[2]
    assert set_vars["AWS_REGION"] == "us-east-1"
    assert set_vars["ACCOUNT_ID"] == "123"
    assert set_vars["AGENT_ID"] == "agent-1"
    assert set_vars["CONNECTION_ID"] == "c1"


def test_add_repo_stamps_connection_id_and_agp_api_url(project_service_fakes):
    """The runtime-build trigger job needs BOTH CONNECTION_ID (org resolution at
    /builds/runtime) and AGP_API_URL (where to POST). CONNECTION_ID is stamped per-repo
    from the project's connection; AGP_API_URL rides through as a platform default. A
    stale repo_override MUST NOT shadow the real connection id."""
    svc = project_service_fakes
    svc._repo_vars["AGP_API_URL"] = "https://api.example/dev/api/v1"
    _make_project(svc)

    _add_and_materialize(
        svc,
        project_id="proj-1",
        name="my-agent",
        template_name=TEMPLATE_NAME,
        agent_config=VALID_AGENT_CONFIG,
        created_by="op@x",
        principal=_principal(),
        repo_overrides={"CONNECTION_ID": "attacker-supplied"},
    )

    set_vars = svc._rollout.set_ci_vars.call_args.args[2]
    assert set_vars["CONNECTION_ID"] == "c1"
    assert set_vars["AGP_API_URL"] == "https://api.example/dev/api/v1"


_EXPECTED_PUSHED_TREE = dict(_SCAFFOLD_FILES)


_REPO_TREE = {
    "src/main.py": b"# the template as the customer ITERATED it\n",
    ".github/workflows/build.yml": b"name: build\non:\n  push:\n    branches: [main]\n# tuned\n",
    "Dockerfile": b"FROM python:3.13-slim\n",
    "README.md": b"# agent (customer's own words)\n",
    "src/tools/their_tool.py": b"# a tool the seed never had\n",
}


_TEMPLATE_HEAD_SHA = "3e2b70b6c1d4e5f60718293a4b5c6d7e8f901234"


_REPO_TREE_AFTER_A_LATER_PUSH = {**_REPO_TREE, "src/main.py": b"# pushed mid-materialize\n"}


_TEMPLATE_LATER_SHA = "9f8e7d6c5b4a39281706f5e4d3c2b1a098765432"


class _FakeGit:
    """A MINIMAL git model — commits carrying a content snapshot, branch refs, and a real 3-way merge."""

    def __init__(self):
        self.commits = {}
        self.branches = {}
        self.messages = []
        self._n = 0

    def _commit(self, tree, parents):
        self._n += 1
        sha = f"c{self._n}"
        self.commits[sha] = {"tree": dict(tree), "parents": list(parents)}
        return sha

    def commit_tree(self, branch, tree, message=""):
        """Replace ``branch``'s whole tree in ONE commit — the ``commit_files`` shape."""
        tip = self.branches.get(branch)
        if tip is not None and self.commits[tip]["tree"] == dict(tree):
            return tip
        sha = self._commit(tree, [tip] if tip else [])
        self.branches[branch] = sha
        self.messages.append(message)
        return sha

    def init(self, files):
        """The template copy: `main`'s root commit."""
        self.branches["main"] = self._commit(files, [])

    def branch_from(self, new_branch, from_branch):
        if from_branch not in self.branches:
            raise KeyError(from_branch)
        self.branches.setdefault(new_branch, self.branches[from_branch])

    def write(self, branch, path, content):
        if branch not in self.branches:
            raise KeyError(branch)
        tip = self.branches[branch]
        tree = dict(self.commits[tip]["tree"])
        tree[path] = content
        self.branches[branch] = self._commit(tree, [tip])

    def tree(self, branch):
        return self.commits[self.branches[branch]]["tree"]

    def tree_sha(self, branch):
        """A CONTENT hash, exactly like a git tree sha: two branches whose files match resolve to the
        SAME value
        even though their commit shas differ."""
        return hashlib.sha1(
            repr(sorted(self.tree(branch).items())).encode(), usedforsecurity=False
        ).hexdigest()

    def _ancestors(self, sha):
        seen, stack = set(), [sha]
        while stack:
            s = stack.pop()
            if s in seen:
                continue
            seen.add(s)
            stack.extend(self.commits[s]["parents"])
        return seen

    def merge_base(self, a, b):
        common = self._ancestors(self.branches[a]) & self._ancestors(self.branches[b])
        return max(common, key=lambda s: int(s[1:])) if common else None

    def compare(self, base, head):
        """GitHub's ``compare {base}...{head}`` — ``ahead_by``/``behind_by`` + ``status``."""
        base_anc = self._ancestors(self.branches[base])
        head_anc = self._ancestors(self.branches[head])
        ahead, behind = len(head_anc - base_anc), len(base_anc - head_anc)
        status = "diverged" if ahead and behind else "ahead" if ahead else "behind" if behind else "identical"
        return {"status": status, "ahead_by": ahead, "behind_by": behind}

    def merge(self, into, frm):
        """3-way merge ``frm`` into ``into`` — the real semantics, which is the whole point.

        A path is taken from ``frm`` ONLY where ``frm`` changed it relative to the merge base.
        A path that only ``into`` changed therefore SURVIVES the merge. That is exactly how the
        operator's config stayed on `main` while `dev` kept the template default, and why the
        two trees stayed DIFFERENT after a clean merge (observed live 2026-08-01: `dev` tree
        b9a4ce4e vs `main` tree e7b4967b). A fake that merged by overwriting `into` wholesale
        would hide it."""
        base_tree = self.commits[self.merge_base(into, frm)]["tree"]
        merged = dict(self.tree(into))
        head_tree = self.tree(frm)
        for path, content in head_tree.items():
            if base_tree.get(path) != content:
                merged[path] = content
        for path, content in base_tree.items():
            if path not in head_tree and merged.get(path) == content:
                del merged[path]
        self.branches[into] = self._commit(merged, [self.branches[into], self.branches[frm]])


class _FakeProvider:
    """The provider seam as the real client behaves where it matters to these tests.

    ``create_repo`` auto-inits one seeded commit on a branch AGP did not choose and makes it the
    default, because the git-data API refuses to write to an empty repository; the seeded content
    does not survive ``commit_files`` but the ref does, which is what the trunk-adoption tests need
    to be able to fail. A second ``create_repo`` seeds nothing, like the real idempotent re-run, so
    "a retried materialize writes no second commit" cannot pass on history the fake supplied.
    ``template_repos`` is empty by default, so a test that seeds no template repo exercises the
    not-found arm. ``pushes`` records every ref AGP pushes, in order. Each ``raise`` below mirrors a
    real provider refusal (committing before the repo exists, an empty file set, a named scope), so
    the tests that depend on them can fail."""

    AUTO_INIT_BRANCH = "main"

    def __init__(self):
        self.repo_vars = {}
        self.scoped_writes = []
        self.created_repos = []
        self.deleted_branches = []
        self.default_branch = None
        self.calls = []
        self.git = _FakeGit()
        self.pushes = []
        self.template_repos = {}
        self.read_repo_calls = []
        self.read_tree_calls = []

    def create_repo(self, org, name, *, private, token, base_url=None):
        self.calls.append("create_repo")
        self.created_repos.append((org, name, private))
        if self.AUTO_INIT_BRANCH not in self.git.branches:
            self.git.branches[self.AUTO_INIT_BRANCH] = self.git._commit({"README.md": b"# seeded\n"}, [])
            self.default_branch = self.AUTO_INIT_BRANCH
        return f"https://github.com/{org}/{name}"

    def set_default_branch(self, org, repo, branch, *, token, base_url=None):
        self.calls.append("set_default_branch")
        if branch not in self.git.branches:
            raise GitHubRepoError(
                f"cannot set default branch of '{org}/{repo}' to '{branch}' — no such branch"
            )
        self.default_branch = branch

    def delete_branch(self, org, repo, branch, *, token, base_url=None):
        self.calls.append("delete_branch")
        if branch == self.default_branch:
            raise GitHubRepoError(
                f"refusing to delete '{branch}' on '{org}/{repo}' — it is the default branch"
            )
        self.deleted_branches.append(branch)
        self.git.branches.pop(branch, None)

    def commit_files(self, org, repo, files, *, branch, message, token, base_url=None):
        self.calls.append("commit_files")
        if not self.created_repos:
            raise GitHubRepoError(f"cannot commit to '{org}/{repo}' — the repository was never created")
        if not files:
            raise GitHubRepoError(
                f"refusing to commit an empty file set to '{org}/{repo}' (branch '{branch}')"
            )
        before = self.git.branches.get(branch)
        sha = self.git.commit_tree(branch, files, message)
        if sha != before:
            self.pushes.append(branch)
        return sha

    def seed_template_repo(self, org, repo, *, default_branch="main", trees):
        """Register a template repo whose ``trees`` is ``{ref: {path: bytes}}``.

        The LAST ref inserted is the tip (``head_sha``) — mirroring a push landing on top of
        history, so a stale-ref read shows up as the wrong tree rather than as no tree."""
        self.template_repos[(org, repo)] = {
            "default_branch": default_branch,
            "head_sha": list(trees)[-1],
            "trees": {ref: dict(tree) for ref, tree in trees.items()},
        }

    def seed_empty_template_repo(self, org, repo, *, default_branch="main"):
        """A repo that EXISTS with no commit yet — ``read_repo`` answers a view whose ``head_sha`` is
        EMPTY, and
        there is no tree to read at any ref."""
        self.template_repos[(org, repo)] = {
            "default_branch": default_branch,
            "head_sha": "",
            "trees": {},
        }

    def read_repo(self, org, repo, *, token, base_url=None):
        self.calls.append("read_repo")
        self.read_repo_calls.append((org, repo))
        found = self.template_repos.get((org, repo))
        if found is None:
            return None
        return RepoView(default_branch=found["default_branch"], head_sha=found["head_sha"])

    def read_tree(self, org, repo, *, ref, token, base_url=None):
        self.calls.append("read_tree")
        self.read_tree_calls.append((org, repo, ref))
        if not ref:
            raise GitHubRepoError(f"refusing to read '{org}/{repo}' at a blank ref")
        found = self.template_repos.get((org, repo))
        if found is None:
            raise GitHubRepoError(f"cannot read tree of '{org}/{repo}' — no such repository")
        tree = found["trees"].get(ref)
        if tree is None:
            raise GitHubRepoError(f"cannot read '{org}/{repo}' at ref '{ref}' — no such ref")
        return dict(tree)

    def set_ci_vars(self, org, repo, variables, *, scope, token, base_url=None):
        self.calls.append("set_ci_vars")
        if scope is not None:
            self.scoped_writes.append(scope)
            raise GitHubRepoError(
                f"failed to set CI variable on '{org}/{repo}' (scope '{scope}', HTTP 404) "
                f"— the scope was never created"
            )
        self.repo_vars = dict(variables)


class _FakeTenantService:
    """TenantService double — its ``get`` returns a fixed Tenant-like object with
    ``stages`` (dev/prod). Only ``.stages`` is read by _materialize_repo."""

    def __init__(self, stages):
        self._stages = stages

    def get(self, tenant_id):
        return SimpleNamespace(id=tenant_id, stages=self._stages)


_TENANT_STAGES = {
    "dev": TenantStageConfig(
        account_id="111111111111",
        region="us-east-1",
        ecr_repo_uri="ecr-dev-uri",
        push_role_arn="arn:aws:iam::111111111111:role/push-dev",
        deploy_role_arn="arn:aws:iam::111111111111:role/deploy-dev",
    ),
    "prod": TenantStageConfig(
        account_id="222222222222",
        region="eu-west-1",
        ecr_repo_uri="ecr-prod-uri",
        push_role_arn="arn:aws:iam::222222222222:role/push-prod",
        deploy_role_arn="arn:aws:iam::222222222222:role/deploy-prod",
    ),
}


def _stage_cfg(account_id: str, region: str, label: str) -> TenantStageConfig:
    """A TenantStageConfig with obviously-fake ids, for the open-stages tests below."""
    return TenantStageConfig(
        account_id=account_id,
        region=region,
        ecr_repo_uri=f"ecr-{label}-uri",
        push_role_arn=f"arn:aws:iam::{account_id}:role/push-{label}",
        deploy_role_arn=f"arn:aws:iam::{account_id}:role/deploy-{label}",
    )


class _FakeTemplateRegistry:
    """``TemplateRegistry`` double — the CATALOG materialize dereferences."""

    def __init__(self, records=(), *, raise_on_get=None):
        self._records = {(r.connection_id, r.name): r for r in records}
        self._raise_on_get = raise_on_get
        self.get_calls = []

    def get(self, connection_id, template_id):
        self.get_calls.append((connection_id, template_id))
        if self._raise_on_get is not None:
            raise self._raise_on_get
        return self._records.get((connection_id, template_id))


def _template_record(name=None, *, connection_id="c1", source_org=None, source_repo=None, framework=""):
    """A ``TemplateRecord`` for the catalog double."""
    return TemplateRecord(
        id=name or TEMPLATE_NAME,
        name=name or TEMPLATE_NAME,
        connection_id=connection_id,
        created_at=FIXED.isoformat(),
        source_url="https://example.invalid/some/opaque/path",
        source_org=source_org,
        source_repo=source_repo,
        framework=framework,
    )


@pytest.fixture
def tenant_service_fakes(project_service_fakes):
    """The base fixture reconfigured with a recording provider double + a tenant service."""
    svc = project_service_fakes
    fake_gh = _FakeProvider()
    svc._rollout = fake_gh
    svc._tenants = _FakeTenantService(_TENANT_STAGES)
    return SimpleNamespace(svc=svc, gh=fake_gh)


@pytest.fixture
def repo_source_fakes(tenant_service_fakes):
    """The tenant fixture PLUS a catalog whose ``strands-agentcore`` row is dereferenceable, and a provider
    holding that template repo with real bytes at a real sha."""
    f = tenant_service_fakes
    f.registry = _FakeTemplateRegistry([_template_record(source_org="acme", source_repo="strands-agentcore")])
    f.svc._templates = f.registry
    f.gh.seed_template_repo("acme", "strands-agentcore", trees={_TEMPLATE_HEAD_SHA: _REPO_TREE})
    return f


def _add_and_materialize(svc: ProjectService, **kwargs) -> Repository:
    repo = svc.add_repo(**kwargs)
    svc.run_materialize(repo.id)
    stored = svc.get_repo(repo.id)
    assert stored is not None
    return stored


def _materialized(fakes):
    _make_project(fakes.svc)
    return _add_and_materialize(
        fakes.svc,
        project_id="proj-1",
        name="my-agent",
        template_name=TEMPLATE_NAME,
        agent_config=VALID_AGENT_CONFIG,
        created_by="op@x",
        principal=_principal(),
    )


def test_materialize_makes_exactly_one_tree_write(tenant_service_fakes):
    f = tenant_service_fakes
    _materialized(f)
    assert f.gh.calls == ["create_repo", "commit_files", "set_ci_vars"], f.gh.calls
    assert f.gh.pushes == ["main"], f.gh.pushes
    assert len(f.gh.git.commits) == 2, f.gh.git.commits
    assert f.gh.git.tree("main") == _EXPECTED_PUSHED_TREE


def test_the_push_message_carries_the_skip_ci_marker(tenant_service_fakes):
    """One commit means one CI trigger, so ONE marker suppresses the whole materialize build — on
    every provider, with no branch filters to keep correct.

    Pinned VERBATIM. The marker is the mechanism, not a courtesy: without it the setup push is
    indistinguishable from a developer's, which is the root cause the design names. A message
    missing the marker, or carrying a typo GitHub does not honour, must fail here."""
    f = tenant_service_fakes
    _materialized(f)
    assert f.gh.git.messages == ["chore: initialize from template [skip ci]"], f.gh.git.messages


def test_materialize_pushes_only_the_trunk_and_creates_no_second_branch(tenant_service_fakes):
    """ONE branch."""
    f = tenant_service_fakes
    _materialized(f)
    assert f.gh.pushes == ["main"], f.gh.pushes
    assert list(f.gh.git.branches) == ["main"], f.gh.git.branches


def test_the_pushed_tree_is_exactly_the_template_scaffold(tenant_service_fakes):
    """AGP forwards BYTES and never inspects layout (tenet 5), so a template author may move
    ``build.yml``, restructure directories or add a workflow and it flows through untouched.

    Compared as the WHOLE tree, byte for byte, including nested paths. A subset check would pass
    while silently dropping a file, and dropping ``.github/workflows/build.yml`` would produce a
    repository that looks materialized and never builds."""
    f = tenant_service_fakes
    _materialized(f)
    assert f.gh.git.tree("main") == _EXPECTED_PUSHED_TREE


def test_no_agent_config_file_is_ever_committed(tenant_service_fakes):
    """``agent.config.json`` is DELETED, from the template and from the push."""
    f = tenant_service_fakes
    _materialized(f)
    assert "agent.config.json" not in f.gh.git.tree("main")
    for path, content in f.gh.git.tree("main").items():
        assert b"p1_agent" not in content, f"the operator's config leaked into {path}"


def test_the_operators_config_still_reaches_the_deployed_container(tenant_service_fakes):
    """THE CONTRACT THAT SURVIVES A DELETED MECHANISM — this is why the config commit could go.

    Deleting ``agent.config.json`` is only safe because the operator's configuration reaches the
    runtime by ANOTHER path: the governed registry record (which the buildspec reads via
    ``get-registry-record``), plus AGENT_ID as a repo CI var so the build can name that record.
    This test is the reason the deletion is not a regression, so it must not be deleted with the
    file — it pins the destination, not the vehicle."""
    f = tenant_service_fakes
    _materialized(f)
    created = f.svc._registry.create.call_args.args[0]
    assert created.name == VALID_AGENT_CONFIG["agent_name"]
    assert created.model_id == VALID_AGENT_CONFIG["model_id"]
    assert created.framework == VALID_AGENT_CONFIG["framework"]
    assert f.gh.repo_vars["AGENT_ID"] == "agent-1"


def test_no_prod_candidate_exists_on_a_freshly_materialized_repo(tenant_service_fakes):
    f = tenant_service_fakes
    repo = _materialized(f)

    assert repo.prod_candidate_status is None, (
        "a freshly materialized repo must NOT be promotable to production — "
        f"observed prod_candidate_status={repo.prod_candidate_status!r}"
    )
    assert repo.prod_candidate_image_tag is None
    assert repo.prod_candidate_sha is None
    assert repo.prod_candidate_actor is None
    assert repo.prod_candidate_at is None
    assert all("[skip ci]" in m for m in f.gh.git.messages), f.gh.git.messages


def test_a_retried_materialize_writes_no_second_commit(tenant_service_fakes):
    """Idempotent BY CONTENT — what makes a retried materialize safe rather than merely tolerable.

    Git objects are content-addressed, so re-pushing identical files yields the tree already on the
    branch: no commit, no ref move, and decisively NO push event. Without this, every retry would
    fire a build — and racing builds over one terraform state lock is the live defect this epic
    exists to remove."""
    f = tenant_service_fakes
    repo = _materialized(f)
    before = len(f.gh.git.commits)

    f.svc._pending_materialize[repo.id] = {
        "agent": f.svc._registry.create.return_value,
        "name": "my-agent",
        "connection_id": "c1",
        "template_name": TEMPLATE_NAME,
        "agent_config": VALID_AGENT_CONFIG,
        "repo_overrides": None,
        "tenant_id": "default",
        "trunk_branch": "main",
    }
    for step in f.svc.get_repo(repo.id).steps:
        project_materialize._save_repo_step(f.svc, repo.id, step.key, StepStatus.PENDING)
    f.svc.run_materialize(repo.id)

    assert len(f.gh.git.commits) == before, "a re-push of identical content must write no commit"
    assert f.gh.pushes == ["main"], f"a no-op re-push must fire no push event; {f.gh.pushes}"
    assert f.svc.get_repo(repo.id).cicd_status == "ready"


def test_the_trunk_comes_from_the_project_not_a_literal(tenant_service_fakes):
    """Branch names are project config, so a trunk-based or GitFlow team both work and per-provider default-
    branch conventions stop mattering."""
    f = tenant_service_fakes
    project = f.svc.create_project(
        name="p2",
        connection_id="c1",
        tenant_id="default",
        description="a project whose trunk is not `main`",
        created_by="op@x",
    )
    seed(f.svc._store, _to_item(_PROJECT_PK, project.model_copy(update={"trunk_branch": "release"})))
    _add_and_materialize(
        f.svc,
        project_id=project.id,
        name="my-agent",
        template_name=TEMPLATE_NAME,
        agent_config=VALID_AGENT_CONFIG,
        created_by="op@x",
        principal=_principal(),
    )
    assert f.gh.pushes == ["release"], f.gh.pushes
    assert f.gh.git.tree("release") == _EXPECTED_PUSHED_TREE
    assert list(f.gh.git.branches) == ["release"], f.gh.git.branches
    assert f.gh.default_branch == "release"
    assert f.gh.deleted_branches == ["main"], f.gh.deleted_branches
    assert f.gh.calls.index("set_default_branch") < f.gh.calls.index("delete_branch"), f.gh.calls


def test_the_default_trunk_needs_no_ref_bookkeeping(tenant_service_fakes):
    """The common case must spend nothing. When the trunk IS what ``auto_init`` produced, the repo
    is already correct — so neither adopt call is made, and no ref is deleted.

    The other half of the rule: a fix that unconditionally re-pointed and deleted would add two
    provider calls per materialize for every ordinary project, and a delete against the default
    branch is exactly what the provider refuses."""
    f = tenant_service_fakes
    _materialized(f)
    assert f.gh.calls == ["create_repo", "commit_files", "set_ci_vars"], f.gh.calls
    assert f.gh.deleted_branches == []
    assert f.gh.default_branch == "main"
    assert list(f.gh.git.branches) == ["main"]


def test_a_missing_template_scaffold_fails_the_step_instead_of_pushing_nothing(
    tenant_service_fakes,
):
    """An empty file set would build an EMPTY tree — i.e. delete the branch's contents — so a
    template name with no scaffold on disk must fail LOUDLY and locally.

    The failure is attributed to ``push_template`` (not swallowed, not deferred to the provider's
    less specific refusal), and the record reads ``failed`` rather than ``ready``: a repo that
    reports success with no content is the exact "record says success, reality disagrees" shape this
    epic exists to delete."""
    f = tenant_service_fakes
    _make_project(f.svc)
    repo = _add_and_materialize(
        f.svc,
        project_id="proj-1",
        name="my-agent",
        template_name="no-such-template",
        agent_config=VALID_AGENT_CONFIG,
        created_by="op@x",
        principal=_principal(),
    )
    failed = [s for s in repo.steps if s.status is StepStatus.FAILED]
    assert [s.key for s in failed] == ["push_template"], [(s.key, s.status) for s in repo.steps]
    assert repo.cicd_status == "failed" and repo.status == "failed"
    assert f.gh.pushes == [], f"nothing may be pushed when the scaffold is missing; {f.gh.pushes}"
    assert failed[0].error is not None
    assert "no-such-template" in failed[0].error, failed[0].error
    assert "no files on disk" in failed[0].error, failed[0].error


_TRAVERSAL_NAMES = [
    "../backend/src/core",
    "/etc/ssl",
    "..",
    "../../",
    "strands-agentcore/../../etc",
    "foo/bar",
    ".",
    "./strands-agentcore",
    "",
    "A-Upper",
    "-lead",
    "has_underscore",
    "has.dot",
    "x" * 65,
]


@pytest.mark.parametrize("bad", _TRAVERSAL_NAMES)
def test_a_traversing_template_name_is_REFUSED_not_merely_empty(tenant_service_fakes, bad):
    """Each name must be REFUSED with a clear error — never silently produce an empty file set, and
    never read a
    byte outside the templates directory."""
    f = tenant_service_fakes
    _make_project(f.svc)

    with pytest.raises(ProjectError) as exc:
        f.svc.add_repo(
            project_id="proj-1",
            name="my-agent",
            template_name=bad,
            agent_config=VALID_AGENT_CONFIG,
            created_by="op@x",
            principal=_principal(),
        )
    assert exc.value.kind == "invalid_template_name", (bad, exc.value.kind)
    assert "invalid template name" in exc.value.message, exc.value.message

    f.svc._registry.create.assert_not_called()
    assert f.gh.created_repos == [], f.gh.created_repos
    assert f.gh.pushes == [], f.gh.pushes
    assert f.svc.list_repositories() == [], f.svc.list_repositories()

    with pytest.raises(ProjectError) as deep:
        project_materialize._resolve_scaffold_dir(f.svc, bad)
    assert deep.value.kind == "materialize_error", deep.value.kind


def test_a_symlink_out_of_the_templates_dir_is_refused_by_the_containment_check(tmp_path):
    """LAYER 2, and the case no pattern can see. ``evil-link`` is a perfectly legal template name;
    what makes it dangerous is where the filesystem points it.

    This is why containment is asserted AFTER `resolve()` rather than trusting the regex alone — the
    regex reasons about the string, this reasons about the resulting path. A future refactor that
    loosened the pattern would still be caught here."""
    templates = tmp_path / "agent-templates"
    (templates / "strands-agentcore").mkdir(parents=True)
    (templates / "strands-agentcore" / "main.py").write_bytes(b"# real\n")
    outside = tmp_path / "secrets"
    outside.mkdir()
    (outside / "config.py").write_bytes(b"SECRET=1\n")
    try:
        (templates / "evil-link").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this platform")

    svc = ProjectService(
        table_name="",
        registry=MagicMock(),
        identity=MagicMock(),
        connection_service=MagicMock(),
        github_repo_service=MagicMock(spec=GitHubRepoService),
        agent_templates_dir=str(templates),
    )
    with pytest.raises(ProjectError) as exc:
        project_materialize._resolve_scaffold_dir(svc, "evil-link")
    assert "resolves outside" in exc.value.message
    assert project_materialize._resolve_scaffold_dir(svc, "strands-agentcore").name == "strands-agentcore"


def test_the_refusal_message_never_discloses_the_container_layout(tmp_path):
    """A traversal refusal must not echo a RESOLVED ABSOLUTE PATH. Someone probing for the
    container's layout learns nothing from the error; the name they supplied is already theirs."""
    templates = tmp_path / "agent-templates"
    (templates / "strands-agentcore").mkdir(parents=True)
    svc = ProjectService(
        table_name="",
        registry=MagicMock(),
        identity=MagicMock(),
        connection_service=MagicMock(),
        github_repo_service=MagicMock(spec=GitHubRepoService),
        agent_templates_dir=str(templates),
    )
    with pytest.raises(ProjectError) as exc:
        project_materialize._resolve_scaffold_dir(svc, "../../etc/passwd")
    assert str(templates) not in exc.value.message
    assert "/etc/passwd" not in exc.value.message.replace("'../../etc/passwd'", "")


def test_the_template_name_rule_is_the_catalogs_own_rule(tenant_service_fakes):
    """ONE authority. The pattern is IMPORTED from ``github_template_service``, so a template the
    catalog would accept is one the pusher accepts, and vice versa. Two patterns that could drift is
    exactly how a traversal reopens after someone "tidies" one of them."""
    from services.github_template_service import _NAME_RE
    from services.project_materialize import _TEMPLATE_NAME_RE

    assert _TEMPLATE_NAME_RE is _NAME_RE


def test_the_five_step_keys_are_the_timeline_contract(tenant_service_fakes):
    """The design's step keys, pinned in ORDER (a reorder would scramble the operator's timeline while keeping
    the count intact)."""
    f = tenant_service_fakes
    repo = _materialized(f)
    assert [s.key for s in repo.steps] == [
        "mint_identity",
        "create_repo",
        "push_template",
        "set_repo_vars",
        "provision_langfuse",
        "finalize",
    ]
    assert all(s.status is StepStatus.DONE for s in repo.steps), [
        (s.key, s.error) for s in repo.steps if s.status is not StepStatus.DONE
    ]
    assert {"generate_repo", "commit_config", "create_env_dev", "create_env_prod", "set_env_vars"}.isdisjoint(
        {s.key for s in repo.steps}
    )


def test_a_dereferenceable_template_ships_the_REPOS_bytes_not_the_disk_seed(repo_source_fakes):
    """ARM 1. The record carries both halves of the source pair, so the bytes come from the repo.

    Compared against the WHOLE repo tree byte-for-byte, and the seed tree is asserted absent — the
    two fixtures differ at every path AND the repo has a file the seed lacks, so "shipped the seed"
    cannot pass this by resembling the right answer.

    The sha the tree was read at is pinned too: the tip is resolved ONCE (``read_repo``) and the
    tree is read AT that sha, never at a branch name. That is what stops a template author's push
    landing mid-materialize from contributing half an old tree and half a new one."""
    f = repo_source_fakes
    repo = _materialized(f)

    assert f.gh.git.tree("main") == _REPO_TREE, f.gh.git.tree("main")
    assert f.gh.git.tree("main") != _EXPECTED_PUSHED_TREE
    assert f.gh.read_tree_calls == [("acme", "strands-agentcore", _TEMPLATE_HEAD_SHA)], f.gh.read_tree_calls
    assert f.gh.pushes == ["main"], f.gh.pushes
    assert all(s.status is StepStatus.DONE for s in repo.steps), [
        (s.key, s.error) for s in repo.steps if s.status is not StepStatus.DONE
    ]


def test_the_fetch_step_LABEL_states_which_repo_and_which_sha_shipped(repo_source_fakes):
    """PROVENANCE IS STATED, NOT GUESSED — and the label is where it is stated."""
    f = repo_source_fakes
    repo = _materialized(f)

    step = next(s for s in repo.steps if s.key == "push_template")
    assert step.label == f"Fetch template (strands-agentcore@{_TEMPLATE_HEAD_SHA[:7]})", step.label
    assert step.label != "Push template contents"


def test_the_repo_arm_NEVER_reads_the_seed_directory_even_though_one_exists(repo_source_fakes, monkeypatch):
    """THE NO-SILENT-FALLBACK PIN, asserted on what was NOT read rather than on the output."""
    f = repo_source_fakes

    def _explode(*_a, **_kw):
        raise AssertionError("the repo arm must not read the on-disk seed at all")

    monkeypatch.setattr(project_materialize, "collect_scaffold_files", _explode)
    repo = _materialized(f)

    assert all(s.status is StepStatus.DONE for s in repo.steps), [
        (s.key, s.error) for s in repo.steps if s.status is not StepStatus.DONE
    ]
    assert f.gh.git.tree("main") == _REPO_TREE


def test_a_MISSING_template_repo_fails_the_step_instead_of_shipping_the_seed(repo_source_fakes):
    """ARM 3, AND THE HEART OF THE EPIC. The record points at a repo that is GONE.

    The step FAILS. It does not fall back to disk — even though the seed is right there and would
    produce a working repository — because a customer who deleted or renamed their template repo
    and then got starter bytes with a green timeline has been lied to. "Disk as factory reset" is
    an explicit operator action on the reconcile surface, never an automatic degradation.

    The MESSAGE is pinned: it names the repo and tells the operator the two real remedies, so the
    failure is actionable rather than merely honest."""
    f = repo_source_fakes
    f.gh.template_repos.clear()

    repo = _materialized(f)

    failed = [s for s in repo.steps if s.status is StepStatus.FAILED]
    assert [s.key for s in failed] == ["push_template"], [(s.key, s.status) for s in repo.steps]
    assert failed[0].error == (
        "template repo strands-agentcore missing — re-seed or deregister on the Templates page"
    ), failed[0].error
    assert repo.cicd_status == "failed" and repo.status == "failed"
    assert f.gh.pushes == [], f.gh.pushes
    assert "commit_files" not in f.gh.calls, f.gh.calls
    assert f.gh.read_tree_calls == [], f.gh.read_tree_calls


def test_an_EMPTY_template_repo_is_a_failure_not_a_blank_ref_read(repo_source_fakes):
    """The edge case that would corrupt quietly."""
    f = repo_source_fakes
    f.gh.template_repos.clear()
    f.gh.seed_empty_template_repo("acme", "strands-agentcore")

    repo = _materialized(f)

    failed = [s for s in repo.steps if s.status is StepStatus.FAILED]
    assert [s.key for s in failed] == ["push_template"], [(s.key, s.status) for s in repo.steps]
    assert failed[0].error == (
        "template repo strands-agentcore exists but has no commits — nothing to ship — "
        "re-seed or deregister on the Templates page"
    ), failed[0].error
    assert "missing" not in failed[0].error, failed[0].error
    assert f.gh.read_tree_calls == [], f"a blank ref was forwarded: {f.gh.read_tree_calls}"
    assert f.gh.pushes == [], f.gh.pushes


def test_a_template_repo_whose_TREE_IS_EMPTY_at_a_real_sha_fails_attributably(repo_source_fakes):
    """A repo that EXISTS, has a real commit, and whose tree reads EMPTY."""
    f = repo_source_fakes
    f.gh.template_repos.clear()
    f.gh.seed_template_repo("acme", "strands-agentcore", trees={_TEMPLATE_HEAD_SHA: {}})

    repo = _materialized(f)

    failed = [s for s in repo.steps if s.status is StepStatus.FAILED]
    assert [s.key for s in failed] == ["push_template"], [(s.key, s.status) for s in repo.steps]
    assert failed[0].error == (
        f"template repo strands-agentcore has no files at {_TEMPLATE_HEAD_SHA[:7]} — nothing to push"
    ), failed[0].error
    assert f.gh.read_tree_calls == [("acme", "strands-agentcore", _TEMPLATE_HEAD_SHA)], f.gh.read_tree_calls
    assert f.gh.pushes == [], f.gh.pushes
    assert "commit_files" not in f.gh.calls, f.gh.calls
    assert repo.cicd_status == "failed" and repo.status == "failed"


def test_the_tree_is_read_at_the_RESOLVED_TIP_not_at_a_later_push(repo_source_fakes):
    """The mixed-tree guard, exercised over a repo holding TWO refs.

    The later ref is what a template author's push landing mid-materialize looks like. Because the
    tip is resolved once and the tree is read AT that sha, the newer tree cannot contribute — and a
    tree assembled from both refs never existed as a commit. The fake keys content by (path, ref)
    specifically so this can fail; with one tree per repo it could not."""
    f = repo_source_fakes
    f.gh.template_repos.clear()
    f.gh.seed_template_repo(
        "acme",
        "strands-agentcore",
        trees={
            _TEMPLATE_LATER_SHA: _REPO_TREE_AFTER_A_LATER_PUSH,
            _TEMPLATE_HEAD_SHA: _REPO_TREE,
        },
    )

    _materialized(f)

    assert f.gh.git.tree("main")["src/main.py"] == _REPO_TREE["src/main.py"]
    assert f.gh.git.tree("main") == _REPO_TREE
    assert f.gh.read_tree_calls == [("acme", "strands-agentcore", _TEMPLATE_HEAD_SHA)], f.gh.read_tree_calls


def test_a_record_with_NO_source_pair_falls_back_to_the_seed_and_SAYS_SO(tenant_service_fakes):
    """ARM 2."""
    f = tenant_service_fakes
    f.svc._templates = _FakeTemplateRegistry([_template_record()])

    repo = _materialized(f)

    assert f.gh.git.tree("main") == _EXPECTED_PUSHED_TREE
    step = next(s for s in repo.steps if s.key == "push_template")
    assert step.label == f"Fetch template (seed: {TEMPLATE_NAME})", step.label
    assert f.gh.read_repo_calls == [], f.gh.read_repo_calls
    assert f.gh.read_tree_calls == [], f.gh.read_tree_calls


@pytest.mark.parametrize(
    "source_org,source_repo",
    [("acme", None), (None, "strands-agentcore"), ("", "strands-agentcore"), ("acme", "")],
)
def test_only_BOTH_halves_make_a_record_dereferenceable(tenant_service_fakes, source_org, source_repo):
    """The pin, enforced at the CONSUMER."""
    f = tenant_service_fakes
    f.svc._templates = _FakeTemplateRegistry(
        [_template_record(source_org=source_org, source_repo=source_repo)]
    )

    repo = _materialized(f)

    assert f.gh.git.tree("main") == _EXPECTED_PUSHED_TREE
    step = next(s for s in repo.steps if s.key == "push_template")
    assert step.label == f"Fetch template (seed: {TEMPLATE_NAME})", step.label
    assert f.gh.read_repo_calls == [], f.gh.read_repo_calls


def test_an_UNREGISTERED_template_name_falls_back_to_the_seed(tenant_service_fakes):
    """A name with no catalog row at all."""
    f = tenant_service_fakes
    registry = _FakeTemplateRegistry([])
    f.svc._templates = registry

    repo = _materialized(f)

    assert f.gh.git.tree("main") == _EXPECTED_PUSHED_TREE
    step = next(s for s in repo.steps if s.key == "push_template")
    assert step.label == f"Fetch template (seed: {TEMPLATE_NAME})", step.label
    assert f.gh.read_repo_calls == [], f.gh.read_repo_calls
    assert set(registry.get_calls) == {("c1", TEMPLATE_NAME)}, registry.get_calls


def test_an_UNREADABLE_catalog_FAILS_ADD_REPO_rather_than_degrading_to_the_seed(
    tenant_service_fakes,
):
    """ "AGP could not look" is not evidence of absence — the same rule ``read_repo`` follows."""
    f = tenant_service_fakes
    f.svc._templates = _FakeTemplateRegistry(raise_on_get=RuntimeError("ddb unavailable"))
    _make_project(f.svc)

    with pytest.raises(RuntimeError, match="ddb unavailable"):
        f.svc.add_repo(
            project_id="proj-1",
            name="my-agent",
            template_name=TEMPLATE_NAME,
            agent_config=VALID_AGENT_CONFIG,
            created_by="op@x",
            principal=_principal(),
        )

    f.svc._registry.create.assert_not_called()
    assert f.svc.list_repositories() == []
    assert f.gh.pushes == [], f.gh.pushes


def test_a_catalog_fault_DURING_materialize_still_fails_the_step(tenant_service_fakes):
    """The background half of the rule above, kept alive on the only path that can still reach it.

    A store that answers ``add_repo``'s validation read and then dies is a real sequence (the two
    reads are seconds apart, either side of the 202), and it is the one where the fault has to
    surface as a FAILED STEP: the response is already sent, so ``run_materialize`` cannot raise.
    The seed arm must not be the fallback there either — same reasoning, different layer.

    Driven with a registry that fails on the SECOND call, which is what makes this test about the
    materialize arm specifically rather than a duplicate of the one above."""

    class _FailsOnSecondGet(_FakeTemplateRegistry):
        def get(self, connection_id, template_id):
            if len(self.get_calls) >= 1:
                self.get_calls.append((connection_id, template_id))
                raise RuntimeError("ddb unavailable")
            return super().get(connection_id, template_id)

    f = tenant_service_fakes
    f.svc._templates = _FailsOnSecondGet([_template_record(framework="strands")])

    repo = _materialized(f)

    failed = [s for s in repo.steps if s.status is StepStatus.FAILED]
    assert [s.key for s in failed] == ["push_template"], [(s.key, s.status) for s in repo.steps]
    assert f.gh.pushes == [], f.gh.pushes


def test_with_NO_catalog_wired_at_all_materialize_still_seeds(tenant_service_fakes):
    """The unconfigured/legacy construction (no ``template_registry`` injected) — the pre-28C
    world, where there is no catalog to dereference rather than a catalog that says nothing.

    Kept working deliberately: every existing test construction in this file omits the registry,
    and the seed arm is the correct answer for a service with no pointer store. This is NOT the
    same as an unreadable catalog above — nothing failed, so nothing should fail."""
    f = tenant_service_fakes
    assert f.svc._templates is None

    repo = _materialized(f)

    assert f.gh.git.tree("main") == _EXPECTED_PUSHED_TREE
    step = next(s for s in repo.steps if s.key == "push_template")
    assert step.label == f"Fetch template (seed: {TEMPLATE_NAME})", step.label


def test_a_retried_materialize_re_reads_the_repo_and_converges(repo_source_fakes):
    """Resume safety on the repo arm."""
    f = repo_source_fakes
    f.gh.template_repos.clear()
    repo = _materialized(f)
    assert repo.cicd_status == "failed"

    f.gh.seed_template_repo("acme", "strands-agentcore", trees={_TEMPLATE_HEAD_SHA: _REPO_TREE})
    f.svc.retry_materialize(repo.id)
    f.svc.run_materialize(repo.id)
    retried = f.svc.get_repo(repo.id)

    assert all(s.status is StepStatus.DONE for s in retried.steps), [
        (s.key, s.error) for s in retried.steps if s.status is not StepStatus.DONE
    ]
    assert f.gh.git.tree("main") == _REPO_TREE
    assert f.gh.pushes == ["main"], f.gh.pushes
    step = next(s for s in retried.steps if s.key == "push_template")
    assert step.label == f"Fetch template (strands-agentcore@{_TEMPLATE_HEAD_SHA[:7]})", step.label


def test_the_stage_scoped_vars_are_written_repo_level(tenant_service_fakes):
    """The three keys the template's build reads MUST be present, sourced from the tenant's `dev`
    stage, in the REPOSITORY-wide set.

    Repo-level resolves correctly under the template's ``environment: dev`` jobs (GitHub looks a
    variable up environment-first and falls back to the repository set). Reverting to env scope
    would need an environment AGP no longer creates, so the write would 404."""
    f = tenant_service_fakes
    _materialized(f)
    assert f.gh.repo_vars["ECR_REPOSITORY"] == "ecr-dev-uri"
    assert f.gh.repo_vars["AWS_REGION"] == "us-east-1"
    assert f.gh.repo_vars["AWS_ECR_PUSH_ROLE_ARN"] == "arn:aws:iam::111111111111:role/push-dev"


def test_prods_push_role_never_reaches_ci(tenant_service_fakes):
    """The security property, not an incidental consequence: GitHub is NEVER trusted with the
    production account.

    CI pushes to the dev registry and POSTs ``stage: 'dev'``; a prod deploy is AGP copying an
    approved digest into the prod account. So no prod credential may appear in any repository
    variable — asserted over EVERY value, because the leak that matters is one prod ARN in a key
    nobody thought to check."""
    f = tenant_service_fakes
    _materialized(f)
    for key, value in f.gh.repo_vars.items():
        assert "prod" not in str(value), f"a prod value reached CI via {key}={value!r}"
        assert "222222222222" not in str(value), f"the prod account reached CI via {key}"


def test_materialize_writes_no_scoped_ci_vars(tenant_service_fakes):
    """``scope=None`` only."""
    f = tenant_service_fakes
    _materialized(f)
    assert f.gh.scoped_writes == [], f.gh.scoped_writes


def test_a_tenant_with_no_dev_stage_fails_loudly_naming_its_stages(tenant_service_fakes):
    """A tenant carrying stages but no ``dev`` cannot satisfy the template's ``environment: dev``,
    so materialize REFUSES rather than substituting another stage.

    Substituting is the tempting bug: a tenant with ``staging``+``prod`` would get staging's push
    role wired into a workflow labelled `dev`, and the only evidence would be a deploy landing in
    the wrong account. The error names the stages the tenant ACTUALLY has, because the fix is a
    tenant config change. Fails the ``set_repo_vars`` STEP — the correct blast radius: the repo and
    its template exist, so a retry after fixing the tenant resumes from here."""
    f = tenant_service_fakes
    f.svc._tenants = _FakeTenantService(
        {
            "staging": _stage_cfg("333333333333", "eu-central-1", "staging"),
            "prod": _stage_cfg("222222222222", "eu-west-1", "prod"),
        }
    )
    _make_project(f.svc)
    repo = _add_and_materialize(
        f.svc,
        project_id="proj-1",
        name="my-agent",
        template_name=TEMPLATE_NAME,
        agent_config=VALID_AGENT_CONFIG,
        created_by="op@x",
        principal=_principal(),
    )
    failed = [s for s in repo.steps if s.status is StepStatus.FAILED]
    assert [s.key for s in failed] == ["set_repo_vars"], [(s.key, s.status) for s in repo.steps]
    assert repo.cicd_status == "failed"
    hint = failed[0].error
    assert hint is not None
    assert "dev" in hint and "staging" in hint and "prod" in hint, hint
    assert "default" in hint, hint
    assert "ghp_secret" not in hint
    assert "ghp_secret" not in repo.model_dump_json()
    assert "ecr-staging-uri" not in str(f.gh.repo_vars)
    assert f.gh.git.tree("main") == _EXPECTED_PUSHED_TREE


def test_an_arbitrary_exception_never_puts_its_message_on_the_record(tenant_service_fakes):
    """THE FENCE around the design(4)."""
    f = tenant_service_fakes
    f.gh.commit_files = MagicMock(
        side_effect=RuntimeError("401 for https://api.github.com — token ghp_SECRET123 rejected")
    )
    _make_project(f.svc)
    repo = _add_and_materialize(
        f.svc,
        project_id="proj-1",
        name="my-agent",
        template_name=TEMPLATE_NAME,
        agent_config=VALID_AGENT_CONFIG,
        created_by="op@x",
        principal=_principal(),
    )
    step = next(s for s in repo.steps if s.key == "push_template")
    assert step.status is StepStatus.FAILED
    assert step.error == "RuntimeError", step.error
    assert "ghp_SECRET123" not in repo.model_dump_json()
    assert "api.github.com" not in repo.model_dump_json()


def test_the_per_org_push_role_WINS_over_the_tenant_stage_value(tenant_service_fakes):
    """A TenantService IS wired (the production shape) and the repo's connection carries a
    per-org role — so THAT ARN must be stamped, not the tenant stage's shared one."""
    f = tenant_service_fakes
    f.svc._conn.get_connection.return_value = SimpleNamespace(
        org="OrgB",
        base_url=None,
        ecr_push_role_arn="arn:aws:iam::111111111111:role/agp-cp-dev-ecr-push-OrgB",
    )
    _materialized(f)
    assert (
        f.gh.repo_vars["AWS_ECR_PUSH_ROLE_ARN"] == "arn:aws:iam::111111111111:role/agp-cp-dev-ecr-push-OrgB"
    )
    assert f.gh.repo_vars["AWS_ECR_PUSH_ROLE_ARN"] != "arn:aws:iam::111111111111:role/push-dev"
    assert f.gh.repo_vars["ECR_REPOSITORY"] == "ecr-dev-uri"
    assert f.gh.repo_vars["AWS_REGION"] == "us-east-1"


def test_a_connection_with_NO_per_org_role_still_gets_the_tenant_stage_value(
    tenant_service_fakes,
):
    """The fallback half of the same precedence, and it is what keeps the shared role meaningful: an
    inert push-
    role service (unconfigured env) or a older connection has no per-org ARN, so the tenant stage's value
    stands exactly as before."""
    f = tenant_service_fakes
    f.svc._conn.get_connection.return_value = SimpleNamespace(
        org="acme", base_url=None, ecr_push_role_arn=None
    )
    _materialized(f)
    assert f.gh.repo_vars["AWS_ECR_PUSH_ROLE_ARN"] == "arn:aws:iam::111111111111:role/push-dev"


def test_a_tenant_with_no_dev_stage_STILL_fails_even_when_a_per_org_role_exists(
    tenant_service_fakes,
):
    """ORDERING, pinned. The stage lookup runs FIRST and unconditionally, so a genuinely
    misconfigured tenant still fails loudly instead of being silently rescued by a per-org
    ARN — which would leave the repo with a push role and NO registry, i.e. a build that
    fails later and further from the cause. `ECR_REPOSITORY`/`AWS_REGION` have no per-org
    equivalent, so a rescue here could never be complete."""
    f = tenant_service_fakes
    f.svc._tenants = _FakeTenantService({"uat": _stage_cfg("333333333333", "eu-central-1", "uat")})
    f.svc._conn.get_connection.return_value = SimpleNamespace(
        org="OrgB", base_url=None, ecr_push_role_arn="arn:aws:iam::111111111111:role/push-OrgB"
    )
    _make_project(f.svc)
    repo = _add_and_materialize(
        f.svc,
        project_id="proj-1",
        name="my-agent",
        template_name=TEMPLATE_NAME,
        agent_config=VALID_AGENT_CONFIG,
        created_by="op@x",
        principal=_principal(),
    )
    step = next(s for s in repo.steps if s.key == "set_repo_vars")
    assert step.status is StepStatus.FAILED
    assert step.error is not None
    assert "dev" in step.error and "uat" in step.error, step.error


def test_a_repo_override_still_cannot_shadow_the_per_org_push_role(tenant_service_fakes):
    """The security property from
    `test_a_repo_override_cannot_shadow_the_tenants_stage_credentials`, re-asserted against
    the NEW winner. The per-org ARN is stamped AFTER the Class-B override layers, so moving
    the winner did not open a door for an operator-supplied override to name a role in an
    account the tenant does not own."""
    f = tenant_service_fakes
    f.svc._conn.get_connection.return_value = SimpleNamespace(
        org="OrgB", base_url=None, ecr_push_role_arn="arn:aws:iam::111111111111:role/push-OrgB"
    )
    _make_project(f.svc)
    _add_and_materialize(
        f.svc,
        project_id="proj-1",
        name="my-agent",
        template_name=TEMPLATE_NAME,
        agent_config=VALID_AGENT_CONFIG,
        created_by="op@x",
        principal=_principal(),
        repo_overrides={"AWS_ECR_PUSH_ROLE_ARN": "arn:aws:iam::999999999999:role/attacker"},
    )
    assert f.gh.repo_vars["AWS_ECR_PUSH_ROLE_ARN"] == "arn:aws:iam::111111111111:role/push-OrgB"


def test_a_repo_override_cannot_shadow_the_tenants_stage_credentials(tenant_service_fakes):
    """The stage-sourced keys are stamped AFTER the Class-B override layers, so a repo override
    cannot redirect the build's registry or push role.

    Class-B overrides are operator-supplied. If one could set AWS_ECR_PUSH_ROLE_ARN, a repo could
    name a role in an account the tenant does not own — the same reason AGENT_ID/CONNECTION_ID are
    stamped last and are not overridable."""
    f = tenant_service_fakes
    _make_project(f.svc)
    _add_and_materialize(
        f.svc,
        project_id="proj-1",
        name="my-agent",
        template_name=TEMPLATE_NAME,
        agent_config=VALID_AGENT_CONFIG,
        created_by="op@x",
        principal=_principal(),
        repo_overrides={
            "AWS_ECR_PUSH_ROLE_ARN": "arn:aws:iam::999999999999:role/attacker",
            "ECR_REPOSITORY": "attacker-registry",
        },
    )
    assert f.gh.repo_vars["AWS_ECR_PUSH_ROLE_ARN"] == "arn:aws:iam::111111111111:role/push-dev"
    assert f.gh.repo_vars["ECR_REPOSITORY"] == "ecr-dev-uri"


def test_a_tenant_that_carries_neither_dev_nor_prod_still_fails_attributably(
    tenant_service_fakes,
):
    """A tenant carrying neither ``dev`` nor ``prod`` fails at ``set_repo_vars`` with an error that
    names the missing ``dev`` stage and the ``uat`` stage it does carry."""
    f = tenant_service_fakes
    f.svc._tenants = _FakeTenantService({"uat": _stage_cfg("333333333333", "eu-central-1", "uat")})
    _make_project(f.svc)
    repo = _add_and_materialize(
        f.svc,
        project_id="proj-1",
        name="my-agent",
        template_name=TEMPLATE_NAME,
        agent_config=VALID_AGENT_CONFIG,
        created_by="op@x",
        principal=_principal(),
    )
    step = next(s for s in repo.steps if s.key == "set_repo_vars")
    assert step.status is StepStatus.FAILED
    assert step.error is not None
    assert "dev" in step.error and "uat" in step.error, step.error
    assert len(repo.steps) == 6


def test_a_three_stage_tenant_wires_only_dev_into_ci(tenant_service_fakes):
    """A dev+uat+prod tenant must still materialize cleanly, and CI must carry ONLY dev's
    credentials."""
    f = tenant_service_fakes
    f.svc._tenants = _FakeTenantService(
        {
            "dev": _stage_cfg("111111111111", "us-east-1", "dev"),
            "uat": _stage_cfg("333333333333", "eu-central-1", "uat"),
            "prod": _stage_cfg("222222222222", "eu-west-1", "prod"),
        }
    )
    _make_project(f.svc)
    repo = _add_and_materialize(
        f.svc,
        project_id="proj-1",
        name="my-agent",
        template_name=TEMPLATE_NAME,
        agent_config=VALID_AGENT_CONFIG,
        created_by="op@x",
        principal=_principal(),
    )
    assert repo.cicd_status == "ready"
    assert f.gh.repo_vars["ECR_REPOSITORY"] == "ecr-dev-uri"
    assert f.gh.repo_vars["AWS_ECR_PUSH_ROLE_ARN"] == "arn:aws:iam::111111111111:role/push-dev"
    for absent in ("333333333333", "222222222222", "ecr-uat-uri", "ecr-prod-uri"):
        assert absent not in str(f.gh.repo_vars), f"{absent} leaked into CI vars"


def test_add_repo_keeps_agent_and_connection_repo_level(tenant_service_fakes):
    """AGENT_ID/CONNECTION_ID are stamped last and are never overridable."""
    f = tenant_service_fakes
    _make_project(f.svc)
    _add_and_materialize(
        f.svc,
        project_id="proj-1",
        name="my-agent",
        template_name=TEMPLATE_NAME,
        agent_config=VALID_AGENT_CONFIG,
        created_by="op@x",
        principal=_principal(),
    )
    assert f.gh.repo_vars["AGENT_ID"] == "agent-1"
    assert f.gh.repo_vars["CONNECTION_ID"] == "c1"


def test_add_repo_without_tenant_service_falls_back_to_repo_vars(project_service_fakes):
    """Legacy path: tenant_service=None → the platform-default stage keys stay as configured.

    Kept because this path is what a test/legacy construction exercises, and it must still produce
    a usable var-set rather than silently dropping the three build-critical keys."""
    svc = project_service_fakes
    fake_gh = _FakeProvider()
    svc._rollout = fake_gh
    svc._tenants = None
    _make_project(svc)
    _add_and_materialize(
        svc,
        project_id="proj-1",
        name="my-agent",
        template_name=TEMPLATE_NAME,
        agent_config=VALID_AGENT_CONFIG,
        created_by="op@x",
        principal=_principal(),
    )
    assert fake_gh.repo_vars
    assert fake_gh.repo_vars["ECR_REPOSITORY"] == "123456789012.dkr.ecr.us-east-1.amazonaws.com/agp"
    assert fake_gh.repo_vars["AWS_REGION"] == "us-east-1"
    assert fake_gh.scoped_writes == []


def test_add_repo_unknown_project_raises_not_found(project_service_fakes):
    svc = project_service_fakes
    with pytest.raises(ProjectError) as exc:
        svc.add_repo(
            project_id="ghost",
            name="p1",
            template_name=TEMPLATE_NAME,
            agent_config=VALID_AGENT_CONFIG,
            created_by="op@x",
            principal=_principal(),
        )
    assert exc.value.kind == "not_found"
    svc._registry.create.assert_not_called()


def test_add_repo_rejects_a_framework_the_REGISTERED_TEMPLATE_disagrees_with(project_service_fakes):
    """This test used to be ``test_add_repo_rejects_non_strands``, and the rename is the substance of
    the change
    rather than tidying."""
    svc = project_service_fakes
    svc._templates = _FakeTemplateRegistry([_template_record(framework="strands")])
    _make_project(svc)

    with pytest.raises(ValueError, match="does not match"):
        svc.add_repo(
            project_id="proj-1",
            name="p1",
            template_name=TEMPLATE_NAME,
            agent_config={"agent_name": "a", "framework": "langgraph"},
            created_by="op@x",
            principal=_principal(),
        )
    svc._registry.create.assert_not_called()
    svc._rollout.create_repo.assert_not_called()

    repo = svc.add_repo(
        project_id="proj-1",
        name="p1",
        template_name=TEMPLATE_NAME,
        agent_config=VALID_AGENT_CONFIG,
        created_by="op@x",
        principal=_principal(),
    )
    assert repo.cicd_status == "provisioning"
    svc._registry.create.assert_called_once()


def test_add_repo_with_an_unwired_catalog_cannot_check_the_framework(project_service_fakes):
    """The legacy/unwired construction (``_templates is None``) — the pre-28C world, still live.

    AGP has no record to compare against, so the framework arm FOLDS and any value is accepted.
    That is not a hole being tolerated: it is the only truthful answer, and the alternative
    (refuse, or assume ``strands``) would either break every materialize on the on-disk seed path
    or assert a fact about a template AGP never read. ``agent_name`` is deliberately outside the
    fold — asserted here so an unknown framework cannot quietly disable the check that guards two
    account-global AWS names."""
    svc = project_service_fakes
    assert svc._templates is None
    _make_project(svc)

    repo = svc.add_repo(
        project_id="proj-1",
        name="p1",
        template_name=TEMPLATE_NAME,
        agent_config={"agent_name": "a", "framework": "some-unknown-framework"},
        created_by="op@x",
        principal=_principal(),
    )
    assert repo.cicd_status == "provisioning"

    with pytest.raises(ProjectError) as err:
        svc.add_repo(
            project_id="proj-1",
            name="p2",
            template_name=TEMPLATE_NAME,
            agent_config={"agent_name": "1-bad", "framework": "anything"},
            created_by="op@x",
            principal=_principal(),
        )
    assert err.value.kind == "invalid_agent_name"
    assert "invalid agent_config agent_name" in err.value.message


def test_add_repo_happy_path(project_service_fakes):
    svc = project_service_fakes
    _make_project(svc)

    repo = _add_and_materialize(
        svc,
        project_id="proj-1",
        name="p1",
        template_name=TEMPLATE_NAME,
        agent_config=VALID_AGENT_CONFIG,
        created_by="op@x",
        principal=_principal(),
    )

    svc._identity.provision_identity.assert_called_once()
    assert svc._identity.provision_identity.call_args.args[0].agent_arn is None
    cargs = svc._rollout.create_repo.call_args
    assert cargs.args[0] == "acme"
    assert cargs.args[1] == "p1"
    assert cargs.kwargs["token"] == "ghp_secret"
    svc._rollout.set_ci_vars.assert_called_once()
    set_vars = svc._rollout.set_ci_vars.call_args.args[2]
    assert set_vars["AGENT_ID"] == "agent-1"

    assert repo.project_id == "proj-1"
    assert repo.agent_id == "agent-1"
    assert svc._registry.create.call_args.kwargs["project_id"] == repo.project_id
    assert repo.template_name == "strands-agentcore"
    assert repo.repo_url == "https://github.com/acme/p1"
    assert repo.status == "ready"
    assert repo.cicd_status == "ready"
    assert all(s.status == "done" for s in repo.steps)
    assert len(repo.steps) == 6
    assert "ghp_secret" not in repo.model_dump_json()
    assert [r.id for r in svc.list_repositories()] == [repo.id]


def test_add_repo_passes_model_id_from_agent_config(project_service_fakes):
    """The pre-registered Agent carries model_id from agent.config.json, so it reaches the registry
    envelope the
    runtime build reads for its tfvars."""
    svc = project_service_fakes
    _make_project(svc)

    svc.add_repo(
        project_id="proj-1",
        name="p1",
        template_name=TEMPLATE_NAME,
        agent_config=VALID_AGENT_CONFIG,
        created_by="op@x",
        principal=_principal(),
    )

    created = svc._registry.create.call_args.args[0]
    assert created.model_id == VALID_AGENT_CONFIG["model_id"]


def test_no_operator_config_is_committed_to_the_repo(project_service_fakes):
    """REPLACES ``test_add_repo_commits_agent_config_content``."""
    svc = project_service_fakes
    _make_project(svc)
    _add_and_materialize(
        svc,
        project_id="proj-1",
        name="p1",
        template_name=TEMPLATE_NAME,
        agent_config=VALID_AGENT_CONFIG,
        created_by="op@x",
        principal=_principal(),
    )
    pushed = svc._rollout.commit_files.call_args.args[2]
    assert "agent.config.json" not in pushed
    assert not any(b"p1_agent" in content for content in pushed.values())


def test_add_repo_passes_explicit_purpose_to_registry(project_service_fakes):
    svc = project_service_fakes
    _make_project(svc)
    svc.add_repo(
        project_id="proj-1",
        name="p1",
        template_name=TEMPLATE_NAME,
        agent_config=VALID_AGENT_CONFIG,
        purpose="Triage inbound claims",
        created_by="op@x",
        principal=_principal(),
    )
    agent_create = svc._registry.create.call_args.args[0]
    assert agent_create.purpose == "Triage inbound claims"


def test_add_repo_derives_purpose_when_omitted(project_service_fakes):
    """No purpose -> a non-empty, name-derived default (never "" — CreateRegistryRecord
    requires description min length 1)."""
    svc = project_service_fakes
    _make_project(svc)
    svc.add_repo(
        project_id="proj-1",
        name="p1",
        template_name=TEMPLATE_NAME,
        agent_config=VALID_AGENT_CONFIG,
        created_by="op@x",
        principal=_principal(),
    )
    agent_create = svc._registry.create.call_args.args[0]
    assert agent_create.purpose
    assert "p1" in agent_create.purpose
    assert "strands-agentcore" in agent_create.purpose


def test_add_repo_materialize_failure_persists_failed_repo(project_service_fakes):
    """A materialize failure now surfaces in the BACKGROUND run_materialize (after the 202) — it marks the
    failing step + the record 'failed' and NEVER raises."""
    svc = project_service_fakes
    _make_project(svc)
    svc._rollout.create_repo.side_effect = RuntimeError("github boom")

    repo = svc.add_repo(
        project_id="proj-1",
        name="p1",
        template_name=TEMPLATE_NAME,
        agent_config=VALID_AGENT_CONFIG,
        created_by="op@x",
        principal=_principal(),
    )
    svc.run_materialize(repo.id)
    svc._identity.provision_identity.assert_called_once()
    repos = svc.list_repositories()
    assert len(repos) == 1
    assert repos[0].status == "failed"
    assert repos[0].cicd_status == "failed"
    assert repos[0].agent_id == "agent-1"
    assert repos[0].repo_url is None
    by_key = {s.key: s.status for s in repos[0].steps}
    assert by_key["mint_identity"] == "done"
    assert by_key["create_repo"] == "failed"


def test_add_repo_agent_inherits_project_tenant_id(project_service_fakes):
    """The pre-registered agent carries the PROJECT's actual tenant_id — there is no
    ``or "default"`` compatibility shim (repos inherit through the project)."""
    svc = project_service_fakes
    _make_project(svc, tenant_id="ten-9")
    svc.add_repo(
        project_id="proj-1",
        name="p1",
        template_name=TEMPLATE_NAME,
        agent_config=VALID_AGENT_CONFIG,
        created_by="op@x",
        principal=SimpleNamespace(email="op@x", oid="oid-op"),
    )
    agent_create = svc._registry.create.call_args.args[0]
    assert agent_create.tenant_id == "ten-9"


def test_finalize_advances_status_to_ready(project_service_fakes):
    """The success value the field never had."""
    svc = project_service_fakes
    _make_project(svc)

    repo = svc.add_repo(
        project_id="proj-1",
        name="p1",
        template_name=TEMPLATE_NAME,
        agent_config=VALID_AGENT_CONFIG,
        created_by="op@x",
        principal=_principal(),
    )
    assert svc.get_repo(repo.id).status == "provisioning"

    svc.run_materialize(repo.id)

    stored = svc.get_repo(repo.id)
    assert stored.status == "ready"
    assert stored.cicd_status == "ready"


def test_a_failed_materialize_leaves_status_failed(project_service_fakes):
    """The `ready` write must land ONLY on the terminal-success path. A materialize that died
    mid-way must still read "failed" — if `_finalize_repo` ran unconditionally (or the failure
    value were overwritten later) a broken repo would advertise itself as healthy."""
    svc = project_service_fakes
    _make_project(svc)
    svc._rollout.create_repo.side_effect = RuntimeError("github boom")

    repo = svc.add_repo(
        project_id="proj-1",
        name="p1",
        template_name=TEMPLATE_NAME,
        agent_config=VALID_AGENT_CONFIG,
        created_by="op@x",
        principal=_principal(),
    )
    svc.run_materialize(repo.id)

    stored = svc.get_repo(repo.id)
    assert stored.status == "failed"
    assert stored.cicd_status == "failed"


def test_retry_resets_status_back_to_provisioning(project_service_fakes):
    """The cycle must CLOSE: ready|failed → provisioning → ready. `retry_materialize` owns the
    reset to in-flight, so a repo that reads "failed" and is then retried must not keep
    advertising the old terminal value while a new run is under way."""
    svc = project_service_fakes
    _make_project(svc)
    svc._rollout.create_repo.side_effect = RuntimeError("github boom")

    repo = svc.add_repo(
        project_id="proj-1",
        name="p1",
        template_name=TEMPLATE_NAME,
        agent_config=VALID_AGENT_CONFIG,
        created_by="op@x",
        principal=_principal(),
    )
    svc.run_materialize(repo.id)
    assert svc.get_repo(repo.id).status == "failed"

    svc._rollout.create_repo.side_effect = None
    svc.retry_materialize(repo.id)

    assert svc.get_repo(repo.id).status == "provisioning"


def _langfuse_service_fakes(scaffold_dir, langfuse):
    """A materialize-capable ProjectService with `langfuse` injected as the provisioner.

    Deliberately NOT folded into `project_service_fakes`: that fixture leaves the provisioner
    unwired, which is the legacy/unconfigured construction, and a sibling test below asserts THAT
    shape is a clean no-op. Wiring it there by default would delete that case."""
    registry = MagicMock()
    registry.create.return_value = SimpleNamespace(
        id="agent-1", agent_arn=None, name="p1_agent", langfuse_key_secret_name=None
    )
    identity = MagicMock()
    identity.provision_identity.return_value = None
    conn = MagicMock()
    conn.get_connection.return_value = SimpleNamespace(org="acme", base_url=None)
    conn.get_bearer_token.return_value = "ghp_secret"
    github = MagicMock()
    github.create_repo.return_value = "https://github.com/acme/p1"
    ids = iter(["proj-1", "repo-1", "repo-2"])
    svc = ProjectService(
        table_name="",
        registry=registry,
        identity=identity,
        connection_service=conn,
        github_repo_service=github,
        agent_templates_dir=str(scaffold_dir),
        langfuse_provisioning=langfuse,
        repo_vars={
            "AWS_REGION": "us-east-1",
            "AWS_ECR_PUSH_ROLE_ARN": "arn:aws:iam::123456789012:role/ecr-push",
            "ECR_REPOSITORY": "123456789012.dkr.ecr.us-east-1.amazonaws.com/agp",
            "AGP_API_URL": "https://agp.example/api/v1",
        },
        new_id=lambda: next(ids),
        now=lambda: FIXED,
    )
    _make_project(svc)
    return svc


def _materialize_one(svc):
    return _add_and_materialize(
        svc,
        project_id="proj-1",
        name="my-agent",
        template_name=TEMPLATE_NAME,
        agent_config=VALID_AGENT_CONFIG,
        created_by="op@x",
        principal=_principal(),
    )


def test_materialize_provisions_LANGFUSE_and_stamps_the_secret_name_on_the_envelope(scaffold_dir):
    """THE DEFECT THIS CLOSES, asserted on the field the buildspec actually reads.

    `langfuse_key_secret_name` is the ONE value that has to reach the envelope: buildspec.yml
    lifts it out of the registry record into the `langfuse_secret_name` tfvar, the runtime module
    passes it as `LANGFUSE_SECRET_NAME`, and the container resolves the key pair from Secrets
    Manager itself. Nothing else in the chain names the secret, so a None here is the whole
    silent zero — which is why this asserts the FIELD rather than merely that a call was made."""
    langfuse = MagicMock(name="LangfuseProvisioningService")
    agent_seen = {}

    def provision(agent):
        agent.langfuse_project_id = "clx-proj-1"
        agent.langfuse_key_secret_name = f"langfuse-agent-{agent.id}-keys"
        agent_seen["agent"] = agent

    langfuse.provision_agent_project.side_effect = provision

    svc = _langfuse_service_fakes(scaffold_dir, langfuse)
    repo = _materialize_one(svc)

    langfuse.provision_agent_project.assert_called_once()
    agent = agent_seen["agent"]
    assert agent is svc._registry.create.return_value
    assert agent.langfuse_key_secret_name == f"langfuse-agent-{agent.id}-keys"
    step = next(s for s in repo.steps if s.key == "provision_langfuse")
    assert step.status is StepStatus.DONE
    assert repo.cicd_status == "ready"


def test_the_langfuse_secret_name_matches_THE_PROVISIONERS_OWN_derivation(scaffold_dir):
    """The expected name is read OUT of the real provisioner rather than restated.

    The test above spells `langfuse-agent-{id}-keys` in a fake, so on its own it proves the
    materialize step carries whatever the provisioner produces — not that the two agree. Both
    ends are the same string today only because `LangfuseProvisioningService._agent_secret_name`
    says so, and it is the sole producer: it names the secret the provisioner CREATES, the name
    the delete cascade tears down, and the value `langfuse_metrics_service` reads back. A drift
    would be silent in every direction (a container resolving a name nothing wrote)."""
    from services.langfuse_provisioning import LangfuseProvisioningService

    agent = SimpleNamespace(id="agent-1")
    provisioner = LangfuseProvisioningService(
        langfuse_host="https://langfuse.example", langfuse_secret_name="langfuse-admin", region="us-east-1"
    )
    derived = provisioner._agent_secret_name(agent)
    assert derived == f"langfuse-agent-{agent.id}-keys"


def test_a_LANGFUSE_FAILURE_marks_ITS_OWN_row_and_ABORTS_NOTHING(scaffold_dir):
    """BEST-EFFORT, pinned in BOTH directions — which is the entire contract, because either half
    alone is a lie.

    Failing loudly on its own row is direction one: the route's `provision_langfuse_best_effort`
    swallows every failure into a log, and a step reusing THAT wrapper would report `done` on an
    agent that will trace into nothing — the same "record says success, reality disagrees" class
    this epic exists to delete. So the runner calls the provisioner directly and the step row
    fails.

    Aborting nothing is direction two: Langfuse is a third-party service the repository does not
    depend on. A Langfuse outage must not leave a fully-built repo reading `failed` with its
    `finalize` step never run, because `retry_materialize` would then be the only way back to
    `ready` — an operator retrying a repo whose code, CI vars and identity are all already
    correct."""
    langfuse = MagicMock(name="LangfuseProvisioningService")
    langfuse.provision_agent_project.side_effect = RuntimeError("langfuse unreachable")

    svc = _langfuse_service_fakes(scaffold_dir, langfuse)
    repo = _materialize_one(svc)

    steps = {s.key: s for s in repo.steps}
    assert steps["provision_langfuse"].status is StepStatus.FAILED
    assert steps["provision_langfuse"].error == "RuntimeError"
    assert steps["finalize"].status is StepStatus.DONE
    assert repo.cicd_status == "ready"
    assert repo.status == "ready"
    assert [steps[k].status for k in ("mint_identity", "create_repo", "push_template", "set_repo_vars")] == [
        StepStatus.DONE
    ] * 4


def test_a_NON_langfuse_step_failure_STILL_stops_the_run(scaffold_dir, monkeypatch):
    """The best-effort tolerance is scoped to ONE step, and this is the non-vacuity guard for that."""
    langfuse = MagicMock(name="LangfuseProvisioningService")
    svc = _langfuse_service_fakes(scaffold_dir, langfuse)
    monkeypatch.setattr(svc._rollout, "set_ci_vars", MagicMock(side_effect=RuntimeError("boom")))

    repo = _materialize_one(svc)

    steps = {s.key: s for s in repo.steps}
    assert steps["set_repo_vars"].status is StepStatus.FAILED
    assert steps["provision_langfuse"].status is StepStatus.PENDING
    assert steps["finalize"].status is StepStatus.PENDING
    assert repo.cicd_status == "failed"


def test_langfuse_step_is_a_clean_NO_OP_on_a_LANGFUSE_UNCONFIGURED_deployment(scaffold_dir):
    """THE PRODUCTION SHAPE of "no Langfuse", and the one the first cut of this step got wrong.

    A ``None`` provisioner is NOT that shape. ``routes/projects.py`` injects
    ``get_langfuse_service()`` UNCONDITIONALLY, and ``LangfuseProvisioningService`` constructs
    happily with ``LANGFUSE_HOST=""`` — the shipped default, documented in ``core/config.py`` as
    "empty ⇒ not configured". So an unconfigured deployment has a REAL provisioner whose host is
    empty, and calling it raises ``requests.MissingSchema`` while building its first URL. Guarding
    only on ``None`` therefore produced a FAILED row on every single materialize in exactly the
    deployments that have no Langfuse — the red-row-on-every-run outcome the step's own contract
    rules out, because a row operators learn to ignore is how the original silent zero survived
    two epics.

    Uses the REAL class, not a double: the whole defect was that the real object behaves unlike the
    obvious fake (it does not raise on construction, it raises on use), and only the real one can
    express that."""
    from services.langfuse_provisioning import LangfuseProvisioningService

    unconfigured = LangfuseProvisioningService(
        langfuse_host="",
        langfuse_secret_name="",
        region="us-east-1",
        registry=None,
    )
    svc = _langfuse_service_fakes(scaffold_dir, unconfigured)

    repo = _materialize_one(svc)

    step = next(s for s in repo.steps if s.key == "provision_langfuse")
    assert step.status is StepStatus.DONE
    assert step.error is None
    assert repo.cicd_status == "ready"
    assert svc._registry.create.return_value.langfuse_key_secret_name is None


def test_langfuse_step_is_a_clean_NO_OP_when_the_provisioner_is_UNWIRED(project_service_fakes):
    """The legacy/never-injected construction, kept alongside the host-empty case above."""
    svc = project_service_fakes
    assert svc._langfuse is None
    _make_project(svc)

    repo = _materialize_one(svc)

    step = next(s for s in repo.steps if s.key == "provision_langfuse")
    assert step.status is StepStatus.DONE
    assert step.error is None
    assert repo.cicd_status == "ready"


def test_retrying_a_BEST_EFFORT_failure_lands_the_repo_BACK_AT_READY(scaffold_dir, monkeypatch):
    """The live sequence, end to end: a failed best-effort row on a `ready` repo, retried.

    Asserted on the RECORD, not only on the row: a retry that turns the red row green while
    leaving the record at `provisioning` is the stranding, and it is the state a real repo was
    found in. `finalize` is the only writer of `ready`/`cicd_status="ready"`, so retry has to put
    it back in play even though it is `done`."""
    langfuse = MagicMock(name="LangfuseProvisioningService")
    langfuse.provision_agent_project.side_effect = RuntimeError("langfuse unreachable")

    svc = _langfuse_service_fakes(scaffold_dir, langfuse)
    registered = svc._registry.create.return_value
    registered.framework = VALID_AGENT_CONFIG["framework"]
    registered.model_id = VALID_AGENT_CONFIG["model_id"]
    svc._registry.get.return_value = registered

    finalize_calls = []
    real_finalize = project_materialize._finalize_repo

    def counting_finalize(service, repo_id, repo_url):
        finalize_calls.append((repo_id, repo_url))
        return real_finalize(service, repo_id, repo_url)

    monkeypatch.setattr(project_materialize, "_finalize_repo", counting_finalize)

    repo = _materialize_one(svc)

    steps = {s.key: s for s in repo.steps}
    assert steps["provision_langfuse"].status is StepStatus.FAILED
    assert steps["finalize"].status is StepStatus.DONE
    assert repo.status == "ready"
    assert len(finalize_calls) == 1

    langfuse.provision_agent_project.side_effect = None
    retried = svc.retry_materialize(repo.id)
    assert retried.status == "provisioning"
    svc.run_materialize(repo.id)

    final = svc.get_repo(repo.id)
    assert final is not None
    after = {s.key: s for s in final.steps}
    assert after["provision_langfuse"].status is StepStatus.DONE
    assert after["provision_langfuse"].error is None
    assert final.status == "ready"
    assert final.cicd_status == "ready"
    assert all(s.status is StepStatus.DONE for s in final.steps)
    assert len(finalize_calls) == 2


def test_retry_on_an_ALL_DONE_repo_still_refuses_and_changes_NOTHING(scaffold_dir):
    """The non-vacuity guard for the fix. Putting `finalize` back in play must be conditional on
    something ELSE being reset — resetting it unconditionally would turn the double-click /
    stale-UI retry on a healthy repo into a reset-and-rerun, which is precisely what the
    ``nothing_to_retry`` 409 exists to refuse (a route contract pinned in
    ``test_repo_status_retry.py``)."""
    langfuse = MagicMock(name="LangfuseProvisioningService")
    svc = _langfuse_service_fakes(scaffold_dir, langfuse)

    repo = _materialize_one(svc)
    assert all(s.status is StepStatus.DONE for s in repo.steps)

    with pytest.raises(ProjectError) as err:
        svc.retry_materialize(repo.id)
    assert err.value.kind == "nothing_to_retry"

    after = svc.get_repo(repo.id)
    assert after is not None
    assert all(s.status is StepStatus.DONE for s in after.steps)
    assert after.status == "ready"
    assert after.cicd_status == "ready"


def test_the_build_kinds_match_the_infra_template_registrys_closed_set():
    assert {_CONTAINER_BUILD_KIND, _CONFIG_BUILD_KIND} == set(SUPPORTED_BUILD_KINDS)


def test_the_stage_ci_vars_are_exactly_the_three_stage_scoped_keys():
    stage = SimpleNamespace(ecr_repo_uri="123.dkr.ecr/x", region="eu-west-1", push_role_arn="arn:role")

    stage_vars = _build_stage_ci_vars(SimpleNamespace(stages={"dev": stage}), "t-1")

    assert set(stage_vars) == set(_STAGE_SCOPED_VAR_KEYS)


def test_an_mcp_name_with_an_underscore_is_refused_by_the_name_rule():
    assert _OPS_MCP_NAME_RE.match("claims_tools") is None
    assert _OPS_MCP_NAME_RE.match("claims-tools") is not None


def test_the_longest_admitted_mcp_name_fits_the_gateway_role_in_iams_64_character_cap():
    """IAM caps a role name at 64 characters
    (https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_iam-quotas.html); the gateway
    module renders ``agp-<mcp_name>-<stage>-gateway-role``, so the name rule's upper bound must leave
    room for the stage."""
    main_tf = (
        Path(__file__).resolve().parents[2] / "infrastructure" / "modules" / "agentcore_gateway" / "main.tf"
    )
    templates = re.findall(r'^\s*role_name\s*=\s*"([^"]+)"\s*$', main_tf.read_text(), re.M)
    assert templates == ["agp-${var.mcp_name}-${var.stage}-gateway-role"]
    longest = "a" + "b" * 30
    assert _OPS_MCP_NAME_RE.match(longest) is not None
    assert _OPS_MCP_NAME_RE.match(longest + "b") is None

    for stage in ("dev", "prod", "staging"):
        role = templates[0].replace("${var.mcp_name}", longest).replace("${var.stage}", stage)
        assert "${" not in role
        assert len(role) <= 64, role
