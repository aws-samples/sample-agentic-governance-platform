"""Service tests for ProjectService — materialize over the portable provider seam (E28B/T3).

A ``Project`` is an EMPTY container (name + org, no agent/repo). The one-shot
materialize+pre-register logic lives on :meth:`ProjectService.add_repo`, and E28B rewrote what
materialize DOES: ``create_repo`` (an empty repo) → ``commit_files`` (the WHOLE template, ONE
commit, ``[skip ci]``) → ``set_ci_vars``. The E20 enrollment gate is GONE — a repo materializes
without any enroll record.

WHAT THIS FILE'S E28B TESTS ARE FOR, and why the old ones had to go. The pre-E28B path made SIX
writes to a brand-new repository and four live defects came out of that; four fixes only changed
which writer won. So this file used to be full of ORDERING and BRANCH-SHAPE assertions — the
config lands on `dev`, `main` stays pristine, `dev` is exactly one commit ahead, a clean merge
reconverges the trees. Every one of those describes a relationship between writes that **no longer
both exist**, so they are deleted rather than adapted (each named in the T3 report). What survives
is the CONTRACT they were protecting, re-pinned against the new mechanism: the operator's
configuration still reaches the deployed container, no materialize push fires a build, and a fresh
repo is still not promotable to production.

``_FakeGit`` is KEPT and still load-bearing: it models per-branch trees and CONTENT-addressed tree
shas, which is what makes "the pushed tree is exactly the scaffold" and "a re-push writes no second
commit" able to fail at all.

All collaborators are faked (``MagicMock``/``SimpleNamespace``) — NO live AWS, Entra, or
GitHub. Mirrors ``test_connection_service``'s in-memory (``table_name=""``) + injected
clock/id style; collaborators are ``MagicMock``.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, call

import pytest
from botocore.exceptions import ClientError

from models.agent import DataClassification
from models.project import Project
from models.repository import (
    RepoDeletePreview,
    RepoDeleteSelection,
    Repository,
    StepStatus,
)
from models.tenant import TenantStageConfig
from services.connection_service import ConnectionError
from services.github_repo_service import GitHubRepoError, GitHubRepoService
from services.project_service import ProjectError, ProjectService, StageUnresolvedError
from services.repo_provider import RepoView
from services.template_registry import TemplateRecord
from services.tenant_credentials import TenantCredentialsError

FIXED = datetime(2026, 7, 2, tzinfo=timezone.utc)

VALID_AGENT_CONFIG = {
    "agent_name": "p1_agent",
    "framework": "strands",
    "model_id": "us.anthropic.claude-sonnet-4-6",
}


def _principal(oid="O1", email="e@x"):
    """A minimal principal — apply_creator_sponsor reads only .oid / .email."""
    return SimpleNamespace(oid=oid, email=email)


# E28B/T3: the template scaffold ON DISK. ``push_template`` reads it with
# ``collect_scaffold_files`` and pushes those exact bytes, so the tests need a real directory —
# this is the INPUT half of the "what AGP pushes is what the template contains" contract, and a
# mocked-out file read could not express it. Nested paths are deliberate: the push must carry
# directory structure through untouched (a template author may restructure freely).
_SCAFFOLD_FILES = {
    "src/main.py": b"# the template's agent\n",
    ".github/workflows/build.yml": b"name: build\non:\n  push:\n    branches: [main]\n",
    "Dockerfile": b"FROM python:3.11-slim\n",
    "README.md": b"# agent\n",
}

TEMPLATE_NAME = "strands-agentcore"


@pytest.fixture(scope="session")
def scaffold_dir(tmp_path_factory):
    """A real on-disk template scaffold, written once per session."""
    root = tmp_path_factory.mktemp("agent-templates")
    for rel, content in _SCAFFOLD_FILES.items():
        path = root / TEMPLATE_NAME / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return root


@pytest.fixture
def project_service_fakes(scaffold_dir):
    registry = MagicMock()
    # registry.create returns the pre-registered record — arn is None (identity
    # only, no runtime yet). ``provision_identity`` must see this exact object.
    registry.create.return_value = SimpleNamespace(id="agent-1", agent_arn=None, name="p1_agent")

    identity = MagicMock()
    identity.provision_identity.return_value = None  # non-awaitable → driven as-is

    conn = MagicMock()
    conn.get_connection.return_value = SimpleNamespace(org="acme", base_url=None)
    conn.get_bearer_token.return_value = "ghp_secret"

    # E28D — `spec=` the provider, so this mock can FAIL. A bare MagicMock auto-creates any
    # attribute a test names, which makes every `svc._rollout.<x>.assert_*` here unfalsifiable:
    # it holds identically whether `<x>` exists, was renamed, or never existed (E28B/T7 found and
    # deleted exactly one such vacuous assertion — see the comment below at the `_rollout` asserts).
    # With `spec=`, an attribute the real class does not have raises AttributeError, so a seam
    # rename reddens the suite instead of silently hollowing it out. Scope, stated so nobody
    # over-trusts it: `spec=<class>` checks attribute NAMES only — the child mocks are not
    # signature-checked (verified: `create_repo('acme')` with 2 missing kwargs still passes), so a
    # signature drift is still caught by `test_repo_provider.py`'s per-method signature comparison,
    # not here. If this ever goes red, fix the TEST's target — never soften `spec=` back to a bare
    # mock.
    github = MagicMock(spec=GitHubRepoService)
    # E28B: the provider seam's create_repo returns the repo URL (a str, not a tuple).
    github.create_repo.return_value = "https://github.com/acme/p1"

    # Distinct ids per call (project first, then repo) so partitions don't alias.
    ids = iter(["proj-1", "repo-1", "repo-2", "repo-3"])

    svc = ProjectService(
        table_name="",  # in-memory local fallback
        registry=registry,
        identity=identity,
        connection_service=conn,
        github_repo_service=github,
        agent_templates_dir=str(scaffold_dir),
        repo_vars={
            "AWS_REGION": "us-east-1",
            "AWS_ECR_PUSH_ROLE_ARN": "arn:aws:iam::123456789012:role/ecr-push",
            "ECR_REPOSITORY": "123456789012.dkr.ecr.us-east-1.amazonaws.com/agp",
        },
        new_id=lambda: next(ids),
        now=lambda: FIXED,
    )
    return svc


def _make_project(svc: ProjectService, tenant_id: str = "default"):
    return svc.create_project(
        name="p1",
        connection_id="c1",
        tenant_id=tenant_id,
        description="the first project",
        created_by="op@x",
    )


def _add_and_materialize(svc: ProjectService, **kwargs):
    """E25C/T2: add_repo now only persists a PENDING record + defers the 8 side effects to
    run_materialize (the BackgroundTask). These service tests assert those side effects, so
    they drive add_repo → run_materialize synchronously and return the reloaded record."""
    repo = svc.add_repo(**kwargs)
    svc.run_materialize(repo.id)
    return svc.get_repo(repo.id)


# --------------------------------------------------------------------------- #
# create_project — empty container
# --------------------------------------------------------------------------- #


def test_create_project_persists_empty_container(project_service_fakes):
    svc = project_service_fakes
    p = _make_project(svc)

    assert p.id == "proj-1"
    assert p.name == "p1"
    assert p.connection_id == "c1"
    assert p.tenant_id == "default"
    assert p.description == "the first project"
    assert p.created_by == "op@x"
    # No agent minted, no repo materialized for an empty container.
    svc._registry.create.assert_not_called()
    svc._identity.provision_identity.assert_not_called()
    svc._rollout.create_repo.assert_not_called()
    assert svc.list_repositories() == []
    # It round-trips as a detail with no repos.
    detail = svc.get_project("proj-1")
    assert detail is not None
    assert detail.project.id == "proj-1"
    assert detail.repositories == []


# --------------------------------------------------------------------------- #
# add_repo — the 5 brief contracts
# --------------------------------------------------------------------------- #


def test_add_repo_creates_the_repo_and_pushes_the_template(project_service_fakes):
    """E28B — the repo is CREATED EMPTY and AGP pushes the template itself.

    Renamed from ``..._generates_from_template_and_commits_config``: both halves of that name
    describe deleted mechanisms (the provider's async template copy, and the config commit). This
    pins the calls that replaced them, on the portable seam."""
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
    # ONE commit carrying the WHOLE scaffold, marked `[skip ci]`, on the project's trunk.
    svc._rollout.commit_files.assert_called_once()
    call = svc._rollout.commit_files.call_args
    assert call.args[:2] == ("acme", "my-agent")
    assert call.args[2] == _EXPECTED_PUSHED_TREE
    assert call.kwargs["branch"] == "main"
    assert call.kwargs["message"] == "chore: initialize from template [skip ci]"
    # E28B/T7 — the `commit_file.assert_not_called()` that stood here was VACUOUS and is deleted.
    # `svc._rollout` is a bare `MagicMock`, so it auto-creates any attribute a test names: the
    # assertion held against a method that had been renamed, deleted, or never existed alike.
    # `commit_file` is now GONE from the real client, and the fence that can actually fail is
    # `test_repo_provider.test_the_pre_e28b_github_shaped_methods_are_GONE`, which reads the CLASS.


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

    # Owner gap CLOSED: the creator sponsors by default (apply_creator_sponsor).
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

    # No enrollment_service on the service at all — the E20 gate is gone.
    assert not hasattr(svc, "_enrollment")

    # A repo materializes without any enroll record / precheck.
    repo = _add_and_materialize(
        svc,
        project_id="proj-1",
        name="my-agent",
        template_name=TEMPLATE_NAME,
        agent_config=VALID_AGENT_CONFIG,
        created_by="op@x",
        principal=_principal(),
    )
    assert repo.status == "ready"  # E28A/T5 — a materialized repo reads terminal-success
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

    # effective = {**platform_defaults, **project_overrides, **repo_overrides, AGENT_ID, CONNECTION_ID}
    set_vars = svc._rollout.set_ci_vars.call_args.args[2]
    assert set_vars["AWS_REGION"] == "us-east-1"  # platform default (ctor repo_vars)
    assert set_vars["ACCOUNT_ID"] == "123"  # repo override
    assert set_vars["AGENT_ID"] == "agent-1"  # always stamped (minted agent id)
    # CONNECTION_ID is stamped from the project's connection_id so the scaffold's
    # runtime-build trigger job can POST it to /builds/runtime (org resolution).
    assert set_vars["CONNECTION_ID"] == "c1"


def test_add_repo_stamps_connection_id_and_agp_api_url(project_service_fakes):
    """The runtime-build trigger job needs BOTH CONNECTION_ID (org resolution at
    /builds/runtime) and AGP_API_URL (where to POST). CONNECTION_ID is stamped per-repo
    from the project's connection; AGP_API_URL rides through as a platform default. A
    stale repo_override MUST NOT shadow the real connection id."""
    svc = project_service_fakes
    # AGP_API_URL is a platform default (settings-sourced) — inject via ctor repo_vars.
    svc._repo_vars["AGP_API_URL"] = "https://api.example/dev/api/v1"
    _make_project(svc)  # connection_id="c1"

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
    assert set_vars["CONNECTION_ID"] == "c1"  # per-repo stamp wins over any override
    assert set_vars["AGP_API_URL"] == "https://api.example/dev/api/v1"


# --------------------------------------------------------------------------- #
# E25/T4 — per-stage GitHub Environment var writes from tenant config
# --------------------------------------------------------------------------- #


# E28B/T3: the tree a materialized repo ends up with is EXACTLY the on-disk scaffold — AGP pushes
# those bytes itself rather than asking the provider to copy a template repo. So the expected tree
# is derived from ``_SCAFFOLD_FILES`` rather than declared separately: two hand-maintained copies of
# "what the template contains" would drift, and the drift would silently weaken every assertion
# that the push carries the template faithfully.
#
# The old ``_TEMPLATE_TREE`` constant contained ``agent.config.json`` because the template shipped
# one and materialize overwrote it. Both are gone (D-B2): the runtime never read that file.
_EXPECTED_PUSHED_TREE = dict(_SCAFFOLD_FILES)

# E28C/T4 (D-C2) — the template REPO's bytes, which is what a dereferenceable record makes
# materialize ship. DELIBERATELY DIFFERENT from the on-disk seed at every path, and it carries a
# file the seed does not have: that difference is the ONLY thing that can distinguish "shipped the
# repo" from "shipped the seed", and the whole epic exists because those two silently diverge.
_REPO_TREE = {
    "src/main.py": b"# the template as the customer ITERATED it\n",
    ".github/workflows/build.yml": b"name: build\non:\n  push:\n    branches: [main]\n# tuned\n",
    "Dockerfile": b"FROM python:3.13-slim\n",
    "README.md": b"# agent (customer's own words)\n",
    "src/tools/their_tool.py": b"# a tool the seed never had\n",
}
_TEMPLATE_HEAD_SHA = "3e2b70b6c1d4e5f60718293a4b5c6d7e8f901234"
# A LATER push onto the same template repo. Read at the tip sha this must never appear; it exists
# so "resolve the tip once, read AT that sha" is a claim that can fail.
_REPO_TREE_AFTER_A_LATER_PUSH = {**_REPO_TREE, "src/main.py": b"# pushed mid-materialize\n"}
_TEMPLATE_LATER_SHA = "9f8e7d6c5b4a39281706f5e4d3c2b1a098765432"


class _FakeGit:
    """A MINIMAL git model — commits carrying a content snapshot, branch refs, and a real
    3-way merge. It exists so the E28A/T3 tree tests CAN FAIL.

    The old ``_FakeGitHub`` stored one flat ``commit_paths`` list with no branch dimension, so
    a commit to `dev` and a commit to `main` were the same event to it. Every branch-shape
    assertion would then have passed against the very implementation that shipped divergent
    trees to production — a fake more generous than reality, which E28 hit three times (T6b,
    T4b, T14). So content here is per-branch: a write to `dev` is INVISIBLE on `main`, and
    ``branch_from`` snapshots the source tip at cut time rather than aliasing it forever.

    Deliberately NOT a git implementation: no packfiles, no rename detection, no conflict
    modelling (the merges these tests perform are clean by construction). Commit ids are
    minted monotonically (``c1``, ``c2``, ...), which is what lets :meth:`merge_base` pick the
    nearest common ancestor by id order — true for this model, NOT for git in general."""

    def __init__(self):
        self.commits = {}  # sha -> {"tree": {path: bytes}, "parents": [sha]}
        self.branches = {}  # branch -> tip sha
        self.messages = []  # E28B: every commit MESSAGE written, in order (the [skip ci] check)
        self._n = 0

    def _commit(self, tree, parents):
        self._n += 1
        sha = f"c{self._n}"
        self.commits[sha] = {"tree": dict(tree), "parents": list(parents)}
        return sha

    def commit_tree(self, branch, tree, message=""):
        """Replace ``branch``'s whole tree in ONE commit — the E28B ``commit_files`` shape.

        IDEMPOTENT BY CONTENT, mirroring the real client's tree-sha gate: if the branch already
        carries this exact tree, NO commit is written and the ref does not move. That is not
        cosmetic fidelity — it is what makes "a retried materialize converges instead of firing a
        second build" a testable claim. A fake that always appended a commit would report success
        for an implementation that pushes twice.

        Creates the branch when it does not exist (a root commit), which is what lets the trunk be
        project config rather than whatever the provider's auto-init happened to name."""
        tip = self.branches.get(branch)
        if tip is not None and self.commits[tip]["tree"] == dict(tree):
            return tip  # same content ⇒ same tree sha ⇒ nothing to commit, no push event
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
        # Idempotent, mirroring the real client's benign-422 re-run path.
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
        """A CONTENT hash, exactly like a git tree sha: two branches whose files match resolve
        to the SAME value even though their commit shas differ. That equality is precisely what
        E27A derives the image tag from (``{AGENT_ID}-{tree_sha[:7]}``), so a fake that keyed
        off the commit could never express the mechanic under test."""
        return hashlib.sha1(repr(sorted(self.tree(branch).items())).encode()).hexdigest()

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
        status = (
            "diverged" if ahead and behind
            else "ahead" if ahead
            else "behind" if behind
            else "identical"
        )
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
                del merged[path]  # deleted on the head side, untouched on ours
        self.branches[into] = self._commit(merged, [self.branches[into], self.branches[frm]])


class _FakeProvider:
    """``RepoProvider`` double over a real :class:`_FakeGit` (E28B/T3).

    Reshaped from E28A's ``_FakeGitHub``: the methods materialize calls are now the seam's
    (``create_repo`` / ``commit_files`` / ``set_ci_vars``), so a double still offering
    ``generate_from_template``/``commit_file``/``create_environment`` would let a test pass against
    an implementation that never moved onto the seam. The old per-stage-environment recorders are
    gone with the writes they recorded.

    THE PRECONDITIONS ARE THE POINT. Each ``raise`` below mirrors a real provider refusal, and each
    is what makes a class of test able to fail:
      * committing before the repo exists (the real create is what makes the git database exist);
      * an EMPTY file set — the real client refuses it because an empty tree DELETES the branch;
      * a NAMED ``scope`` — GitHub 404s an env-scoped variable write when the environment does not
        exist, and E28B creates none, so this proves materialize passes ``scope=None``.
    A fake more generous than reality is a test that cannot fail — the mistake E28 hit three
    times."""

    # What the REAL ``create_repo`` leaves behind. It passes ``auto_init=True`` — not a nicety: it
    # seeds one commit so the repository's git database EXISTS, because the git-data API that
    # ``commit_files`` is built on answers 409 "Git Repository is empty" otherwise. GitHub names
    # that seeded branch from the ORG's default-branch setting, which AGP does not control.
    #
    # Modelled here because a fake that seeds NOTHING made a whole class of defect invisible: with
    # no pre-existing branch, pushing to any trunk name looked identical, so "the repo ends up with
    # exactly one branch, named the trunk, and it is the default" could not fail. That is a fake
    # more generous than reality — the third time this epic hit it.
    AUTO_INIT_BRANCH = "main"

    def __init__(self):
        self.repo_vars = {}  # the repository-wide variables dict (last write wins)
        self.scoped_writes = []  # any NON-None scope a caller attempted (must stay empty)
        self.created_repos = []  # list of (org, name, private)
        self.deleted_branches = []  # refs the caller reclaimed after the push
        self.default_branch = None  # what the provider would serve PRs / `branches:` filters from
        self.calls = []  # method names, in call order
        self.git = _FakeGit()  # per-branch content + content-addressed tree shas
        # Every ref this client PUSHES, in order. A commit fires a `push` event, and so does a ref
        # creation — the governance test replays `build.yml`'s trigger condition over this list.
        # Scope: these are AGP's OWN writes. E28B makes no provider-internal copy happen at all,
        # which is the property that removed the third writer.
        self.pushes = []  # list of branch names
        # E28C/T1 reads — see ``seed_template_repo`` below. Empty by default, so a test that does
        # NOT seed a template repo exercises the not-found arm.
        self.template_repos = {}  # (org, repo) -> {default_branch, head_sha, trees}
        self.read_repo_calls = []  # (org, repo), in order
        self.read_tree_calls = []  # (org, repo, ref), in order

    def create_repo(self, org, name, *, private, token, base_url=None):
        self.calls.append("create_repo")
        self.created_repos.append((org, name, private))
        # AUTO-INIT, as the real provider does: one seeded commit on a branch AGP did not choose,
        # and that branch is the repository default. The seeded CONTENT does not survive
        # ``commit_files`` (it builds its tree with no ``base_tree``), but the seeded REF does —
        # that surviving ref is the defect this models.
        #
        # IDEMPOTENT, like the real ``create_repo``: an already-existing repository is a benign
        # re-run (a retried materialize must converge), so a second call seeds NOTHING. Without
        # this guard a retry would mint a fresh root commit and re-point the branch, which would
        # make "a retried materialize writes no second commit" pass for the wrong reason — the
        # fake, not the code, would be supplying the history.
        if self.AUTO_INIT_BRANCH not in self.git.branches:
            self.git.branches[self.AUTO_INIT_BRANCH] = self.git._commit(
                {"README.md": b"# seeded\n"}, []
            )
            self.default_branch = self.AUTO_INIT_BRANCH
        return f"https://github.com/{org}/{name}"

    def set_default_branch(self, org, repo, branch, *, token, base_url=None):
        self.calls.append("set_default_branch")
        if branch not in self.git.branches:
            # Mirrors the provider: you cannot point HEAD at a ref that does not exist.
            raise GitHubRepoError(
                f"cannot set default branch of '{org}/{repo}' to '{branch}' — no such branch"
            )
        self.default_branch = branch

    def delete_branch(self, org, repo, branch, *, token, base_url=None):
        self.calls.append("delete_branch")
        if branch == self.default_branch:
            # Mirrors the provider: the default branch cannot be deleted. This is why the ORDER
            # (re-point, then delete) is load-bearing rather than stylistic.
            raise GitHubRepoError(
                f"refusing to delete '{branch}' on '{org}/{repo}' — it is the default branch"
            )
        self.deleted_branches.append(branch)
        self.git.branches.pop(branch, None)

    def commit_files(self, org, repo, files, *, branch, message, token, base_url=None):
        self.calls.append("commit_files")
        if not self.created_repos:
            raise GitHubRepoError(
                f"cannot commit to '{org}/{repo}' — the repository was never created"
            )
        if not files:
            # Mirrors the real client: an empty mapping would build an EMPTY tree, i.e. delete the
            # branch's entire contents, so it fails closed rather than "succeeding" with nothing.
            raise GitHubRepoError(
                f"refusing to commit an empty file set to '{org}/{repo}' (branch '{branch}')"
            )
        before = self.git.branches.get(branch)
        sha = self.git.commit_tree(branch, files, message)
        # A no-op re-push moves no ref, so it fires NO push event. Recording one anyway would
        # hide exactly the duplicate-build regression the idempotence gate exists to prevent.
        if sha != before:
            self.pushes.append(branch)
        return sha

    # ------------------------------------------------------------------ #
    # E28C/T1 reads (D-C2) — materialize's REPO byte source.
    #
    # ``template_repos`` maps ``(org, repo)`` → ``{"default_branch": .., "head_sha": ..,
    # "trees": {ref: {path: bytes}}}``. An org/repo absent from it answers ``None`` (NOT-FOUND,
    # and nothing else — every other failure raises, T1's contract).
    #
    # CONTENT IS KEYED BY (path, ref), NOT by path alone. That is the whole reason this fake can
    # fail: a template author pushing mid-materialize is modelled by ADDING a second ref with a
    # different tree, so an implementation that read "whatever HEAD is now" instead of the sha it
    # resolved would assemble a tree that was never a commit — invisible to a fake with one tree
    # per repo, which is the "fake more generous than reality" mistake this epic hit three times.
    # ------------------------------------------------------------------ #

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
        """A repo that EXISTS with no commit yet — ``read_repo`` answers a view whose
        ``head_sha`` is EMPTY (T1's edge case), and there is no tree to read at any ref."""
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
            return None  # NOT-FOUND, and nothing else.
        return RepoView(default_branch=found["default_branch"], head_sha=found["head_sha"])

    def read_tree(self, org, repo, *, ref, token, base_url=None):
        self.calls.append("read_tree")
        self.read_tree_calls.append((org, repo, ref))
        if not ref:
            # Mirrors T1: an adapter must REFUSE a blank ref rather than substitute one.
            # Substituting is the mixed-tree defect with the guard removed, so a caller that
            # forwards an empty ``head_sha`` must fail here rather than get "whatever HEAD is".
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
            # A named scope must ALREADY EXIST on GitHub; E28B creates none, so a caller passing
            # one would 404 live. Recorded AND raised so the failure is attributable.
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
    """``TemplateRegistry`` double — the CATALOG materialize dereferences (E28C/T4, D-C2).

    Only ``get(connection_id, template_id)`` is exercised. Keyed exactly as the real one is —
    by the (connection_id, template_id) pair, where the id IS the name (``template_id_for`` is
    identity) — so a lookup that forgot to scope by connection reads nothing rather than
    accidentally matching.

    STRICT LIKE THE REAL ONE on a store fault: ``raise_on_get`` makes ``get`` raise, because
    "AGP could not read its catalog" must NOT degrade to the seed arm. A double that returned
    ``None`` on a fault would let exactly that degradation pass.
    """

    def __init__(self, records=(), *, raise_on_get=None):
        self._records = {(r.connection_id, r.name): r for r in records}
        self._raise_on_get = raise_on_get
        self.get_calls = []

    def get(self, connection_id, template_id):
        self.get_calls.append((connection_id, template_id))
        if self._raise_on_get is not None:
            raise self._raise_on_get
        return self._records.get((connection_id, template_id))


def _template_record(name=None, *, connection_id="c1", source_org=None, source_repo=None):
    """A ``TemplateRecord`` for the catalog double. ``source_org``/``source_repo`` BOTH set =
    dereferenceable (the repo arm); anything else = the seed arm (T2's pin).

    ``source_url`` is deliberately set to something that does NOT decompose into the pair — it
    is display-only, and a materialize that ever parsed it would light this up."""
    return TemplateRecord(
        id=name or TEMPLATE_NAME,
        name=name or TEMPLATE_NAME,
        connection_id=connection_id,
        created_at=FIXED.isoformat(),
        source_url="https://example.invalid/some/opaque/path",
        source_org=source_org,
        source_repo=source_repo,
    )


@pytest.fixture
def tenant_service_fakes(project_service_fakes):
    """The base fixture reconfigured with a recording provider double + a tenant service.

    NO template registry is wired here, so every pre-E28C test in this file keeps exercising the
    on-disk SEED arm — which is what D-C2 specifies for a service with no catalog to dereference."""
    svc = project_service_fakes
    fake_gh = _FakeProvider()
    svc._rollout = fake_gh
    svc._tenants = _FakeTenantService(_TENANT_STAGES)
    return SimpleNamespace(svc=svc, gh=fake_gh)


@pytest.fixture
def repo_source_fakes(tenant_service_fakes):
    """The tenant fixture PLUS a catalog whose ``strands-agentcore`` row is dereferenceable, and a
    provider holding that template repo with real bytes at a real sha (E28C/T4 — D-C2 arm 1)."""
    f = tenant_service_fakes
    f.registry = _FakeTemplateRegistry(
        [_template_record(source_org="acme", source_repo="strands-agentcore")]
    )
    f.svc._templates = f.registry
    f.gh.seed_template_repo("acme", "strands-agentcore", trees={_TEMPLATE_HEAD_SHA: _REPO_TREE})
    return f


# --------------------------------------------------------------------------- #
# E28B/T3 (D-B2/D-B5) — materialize is ONE tree write, so there is nothing to race.
#
# These REPLACE E28A/T3's branch-shape suite. That suite pinned relationships between writes
# (`dev` cut before the config commit, `main` left pristine, `dev` exactly one commit ahead, a
# clean merge reconverging the trees) — relationships whose two sides no longer both exist. Each
# deletion is named in the T3 report; what those tests were PROTECTING is re-pinned here against
# the new mechanism:
#   * the operator's configuration still reaches the deployed container (now via the registry
#     record + repo CI vars, never a committed file);
#   * materialize fires NO build (one commit, one `[skip ci]`) — previously three runs, two wasted;
#   * a fresh repo is still not promotable to production;
#   * a retried materialize converges instead of pushing twice.
# --------------------------------------------------------------------------- #


def _materialized(fakes):
    """materialize one repo on the shared tenant fixture, for the E28B tests below."""
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
    """THE HEADLINE CONTRACT of E28B. One writer, therefore no race.

    The pre-E28B path issued SIX writes to a repository the provider was concurrently writing to,
    and every one of the four defects traced to that rather than to their order. Asserted as the
    ORDERED call sequence, not a count: a count of three would also be satisfied by three writes in
    the wrong order, and it is specifically "at most one write that touches the tree" that removes
    the race."""
    f = tenant_service_fakes
    _materialized(f)
    assert f.gh.calls == ["create_repo", "commit_files", "set_ci_vars"], f.gh.calls
    # Exactly ONE commit is AGP's. The other is the provider's own auto-init seed, which is a
    # precondition of the push (the git-data API 409s on a truly empty repo) — counting total
    # commits would make this test a statement about the provider rather than about AGP, so it
    # counts what AGP pushed: one ref update, once.
    assert f.gh.pushes == ["main"], f.gh.pushes
    assert len(f.gh.git.commits) == 2, f.gh.git.commits  # seed + AGP's single commit
    # And the seed does not SURVIVE: the tree is exactly the scaffold, so nothing pre-existing was
    # carried forward (the real client builds its tree with no ``base_tree``).
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
    """D-B5 — ONE branch. A second branch is a second write, and a ref creation fires a build.

    The old path cut `dev` off `main` and flipped the default, hardcoding one methodology AND one
    provider's branch convention. Asserted as an exact list so a second push to the SAME branch
    fails too — two commits on the trunk is the duplicate-build regression, not merely untidy."""
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
    """D-B2 — ``agent.config.json`` is DELETED, from the template and from the push.

    The runtime never read it: the buildspec takes AGENT_NAME/MODEL_ID from the governed registry
    record. It was a decorative copy of state that lives elsewhere, and FOUR findings existed only
    to keep that copy on the right branch. Asserted over the pushed tree rather than over a call
    argument, because the question is what the repository ends up containing."""
    f = tenant_service_fakes
    _materialized(f)
    assert "agent.config.json" not in f.gh.git.tree("main")
    # Nor anywhere else under any name — nothing in the tree carries the operator's config as JSON.
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
    # And the build can FIND that record: AGENT_ID is stamped on the repo's CI vars.
    assert f.gh.repo_vars["AGENT_ID"] == "agent-1"


def test_no_prod_candidate_exists_on_a_freshly_materialized_repo(tenant_service_fakes):
    """E28A/T3's governance test, KEPT — the contract outlives the mechanism it was written for.

    A push to the prod-candidate branch used to register ``prod_candidate_status="pending"`` on a
    brand-new repo: promotable straight to production with a tree no human had reviewed. No check
    was bypassed — the workflow simply cannot tell a setup commit from a merge. E28B closes it
    twice over (the push carries `[skip ci]`, and promotion is now an approved DIGEST that only a
    real merge produces), so the assertion is retained and its second half re-aimed at the marker
    rather than at which branch was pushed."""
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
    # Every push materialize made is marked `[skip ci]`, so no workflow run exists to register one.
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

    # Re-drive the whole materialize (as a retry would, from the first step).
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
        f.svc._save_repo_step(repo.id, step.key, StepStatus.PENDING)
    f.svc.run_materialize(repo.id)

    assert len(f.gh.git.commits) == before, "a re-push of identical content must write no commit"
    assert f.gh.pushes == ["main"], f"a no-op re-push must fire no push event; {f.gh.pushes}"
    assert f.svc.get_repo(repo.id).cicd_status == "ready"


def test_the_trunk_comes_from_the_project_not_a_literal(tenant_service_fakes):
    """D-B5 — branch names are project config, so a trunk-based or GitFlow team both work and
    per-provider default-branch conventions stop mattering.

    A DATA-flow assertion: the project names ``release``, so the push must land there and NOWHERE
    named `main`. An implementation that kept a `main` literal passes every test above (they all
    use the default) and fails only this one.

    DELIBERATELY BELOW THE API, and it must stay there. E36/T15 (item 24) deleted the create-API
    field together with the validator that refused any non-`main` value: the TEMPLATE's workflow
    pins ``branches: [main]``, so a `release` repo would materialize, report ``ready``, and never
    build — and a field whose only legal value was its own default was API noise. That removal is a
    statement about the template, NOT about the platform: the plumbing here is genuinely
    trunk-agnostic and must be PROVEN so, otherwise the day the workflow's branch filter becomes
    generated nobody can tell whether the backend ever supported it. So this drives the STORED
    project record — the one layer where a trunk is still expressible at all."""
    f = tenant_service_fakes
    project = f.svc.create_project(
        name="p2",
        connection_id="c1",
        tenant_id="default",
        description="a project whose trunk is not `main`",
        created_by="op@x",
    )
    # E36/T15: neither the API nor ``create_project`` takes a trunk any more, so the non-default
    # value is written straight onto the stored record — the same seam a future trunk-config path
    # would use, and ``add_repo`` re-reads the project, so materialize sees `release`.
    f.svc._save_project(project.model_copy(update={"trunk_branch": "release"}))
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
    # THE FULL REQUIREMENT (C-2): exactly ONE branch, it is the trunk, and it is the DEFAULT.
    # ``create_repo`` auto-inits a branch AGP did not choose, so all three parts are needed —
    # asserting only the push would pass while the repo carried a stray `main` that was still
    # default, meaning PRs opened against it and `build.yml`'s `branches:` filter never fired on
    # the branch that actually holds the agent's code.
    assert list(f.gh.git.branches) == ["release"], f.gh.git.branches
    assert f.gh.default_branch == "release"
    assert f.gh.deleted_branches == ["main"], f.gh.deleted_branches
    # Re-point BEFORE delete: the provider refuses to delete its own default branch, so the
    # reverse order fails live on every non-default trunk. Pinned as relative call order.
    assert f.gh.calls.index("set_default_branch") < f.gh.calls.index("delete_branch"), f.gh.calls


def test_the_default_trunk_needs_no_ref_bookkeeping(tenant_service_fakes):
    """The common case must spend nothing. When the trunk IS what ``auto_init`` produced, the repo
    is already correct — so neither adopt call is made, and no ref is deleted.

    This is the other half of C-2: a fix that unconditionally re-pointed and deleted would add two
    provider calls per materialize for every ordinary project, and a delete against the default
    branch is exactly what the provider refuses."""
    f = tenant_service_fakes
    _materialized(f)  # the default project trunk is `main`
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
    # Nothing AGP-authored reached the repo. The provider's auto-init seed is still there (the repo
    # was created before the push was attempted), so this asserts on PUSHES, which are AGP's.
    assert f.gh.pushes == [], f"nothing may be pushed when the scaffold is missing; {f.gh.pushes}"
    # ATTRIBUTED LOCALLY. The provider would also refuse an empty file set, but its message
    # describes the SYMPTOM ("empty file set") while this one names the CAUSE (a template with no
    # scaffold on disk) — the difference between an operator reading "AGP tried to delete the
    # branch" and "that template does not exist here". Pinned on the MESSAGE, so dropping the local
    # guard reddens this: without it the step still fails, but the operator is told the wrong thing.
    assert "no-such-template" in failed[0].error, failed[0].error
    assert "no files on disk" in failed[0].error, failed[0].error


# --------------------------------------------------------------------------- #
# E28B review #1 (CRITICAL) — `template_name` became a FILESYSTEM PATH, so it is
# an arbitrary-file-read that ends in a repository the caller named.
#
# Reproduced before fixing: `base / "/etc/ssl"` → `/etc/ssl` (an absolute segment REPLACES the
# base), and `collect_scaffold_files(base / "../backend/src/core")` harvested 10 real files
# including `config.py` (17,602 bytes) and `security_github_oidc.py` — all of which `commit_files`
# would then push to a repo of the caller's choosing.
#
# Driven by EXECUTION over real strings, not by reading the pattern: this epic's own hardest lesson
# is that three regex bugs in T4 were invisible to reading and only surfaced by running the real
# condition over real inputs.
# --------------------------------------------------------------------------- #


_TRAVERSAL_NAMES = [
    "../backend/src/core",  # the reproduced read of the backend source
    "/etc/ssl",  # absolute — replaces the base entirely
    "..",
    "../../",
    "strands-agentcore/../../etc",  # starts legitimate, escapes later
    "foo/bar",  # any separator at all
    ".",
    "./strands-agentcore",  # resolves INSIDE, but is still not a template name
    "",
    "A-Upper",  # the catalog's own casing rule
    "-lead",
    "has_underscore",
    "has.dot",
    "x" * 65,  # one past the catalog's 64-char ceiling
]


@pytest.mark.parametrize("bad", _TRAVERSAL_NAMES)
def test_a_traversing_template_name_is_REFUSED_not_merely_empty(tenant_service_fakes, bad):
    """Each name must be REFUSED with a clear error — never silently produce an empty file set, and
    never read a byte outside the templates directory.

    "Refused, not empty" is the whole point: an empty harvest would fail the step anyway, but it
    would fail it for the wrong reason and would still have READ the directory to find out.

    E28C/T4 (P-B5) MOVED WHERE THE REFUSAL HAPPENS, and this test moved with it. E28B refused
    inside ``_resolve_scaffold_dir``, i.e. at STEP 3 OF 5 — so every name below was rejected only
    AFTER an Entra identity had been minted and a repository created in the customer's org. The
    traversal was closed; the ORDER was not. ``add_repo`` now refuses at the boundary, so the
    assertion is STRONGER than the one it replaces: not "the step failed" but "nothing happened at
    all". A regression that moved the check back to step 3 would still fail the materialize and
    would still redden this test.

    BOTH LAYERS ARE EXERCISED PER NAME. The boundary refusal is what callers hit; the deeper guard
    is asserted directly below it, because it is the layer that survives a symlink (no pattern can
    see one) and any future change to how that path is built — E28B's review closed a real
    arbitrary-file-read there, and dropping it must not become invisible just because a second
    check now runs first."""
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

    # NOTHING happened: no agent pre-registered, no repo created, no record persisted, no push.
    # Each of these DID happen before P-B5, and each is a cleanup the operator was left with.
    f.svc._registry.create.assert_not_called()
    assert f.gh.created_repos == [], f.gh.created_repos
    assert f.gh.pushes == [], f.gh.pushes
    assert f.svc.list_repositories() == [], f.svc.list_repositories()

    # LAYER 2 still refuses the same name on its own — the containment check is untouched.
    with pytest.raises(ProjectError) as deep:
        f.svc._resolve_scaffold_dir(bad)
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
    except (OSError, NotImplementedError):  # pragma: no cover — platform without symlinks
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
        svc._resolve_scaffold_dir("evil-link")
    assert "resolves outside" in exc.value.message
    # The legitimate sibling still resolves — the guard is not simply refusing everything.
    assert svc._resolve_scaffold_dir("strands-agentcore").name == "strands-agentcore"


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
        svc._resolve_scaffold_dir("../../etc/passwd")
    assert str(templates) not in exc.value.message
    assert "/etc/passwd" not in exc.value.message.replace("'../../etc/passwd'", "")


def test_the_template_name_rule_is_the_catalogs_own_rule(tenant_service_fakes):
    """ONE authority. The pattern is IMPORTED from ``github_template_service``, so a template the
    catalog would accept is one the pusher accepts, and vice versa. Two patterns that could drift is
    exactly how a traversal reopens after someone "tidies" one of them."""
    from services.github_template_service import _NAME_RE

    from services.project_service import _TEMPLATE_NAME_RE

    assert _TEMPLATE_NAME_RE is _NAME_RE


def test_the_five_step_keys_are_the_timeline_contract(tenant_service_fakes):
    """D-B2's step keys, pinned in ORDER (a reorder would scramble the operator's timeline while
    keeping the count intact).

    This REPLACES ``test_the_reorder_did_not_disturb_the_eight_frozen_step_keys``. E28A froze eight
    keys and went to deliberate lengths to keep them; E28B must break that contract because five of
    those steps stop existing. Historical records keep rendering their own stored labels — verified
    in the frontend: both timelines render ``steps[].label`` verbatim and no key→label map exists.

    E28C/T5 adds ``provision_langfuse`` — the one step ADDED rather than removed, because
    ``add_repo`` never did what ``POST /agents`` has done since E26/T4. It is asserted on this
    END-TO-END materialize (not only on the model constant) because the run must reach ``finalize``
    with every row ``done``: this fixture wires no Langfuse provisioner, which is the
    Langfuse-unconfigured deployment, and that has to be a clean no-op rather than a red row on
    every materialize."""
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
    # The deleted keys must not reappear under any code path.
    assert {"generate_repo", "commit_config", "create_env_dev", "create_env_prod", "set_env_vars"}.isdisjoint(
        {s.key for s in repo.steps}
    )


# --------------------------------------------------------------------------- #
# E28C/T4 (D-C2) — MATERIALIZE READS THE TEMPLATE REPO. Repo-first, seed fallback, never silent.
#
# THE DEFECT THIS CLOSES. E28B made materialize push the template's bytes itself, and it read them
# from DISK — the image's baked-in seed. So a customer who registered a template, iterated it in
# their org for six months, and then materialized a new agent got the SEED bytes, silently, and the
# step said "Push template contents" either way. The record claimed the template shipped; the
# repository disagreed. Same class as every other E28 finding.
#
# The fix is that a template record with a STRUCTURAL SOURCE (T2's ``source_org``/``source_repo``
# pair) is DEREFERENCED at use-time: resolve the tip (``read_repo``), read the tree AT that sha
# (``read_tree``), push those bytes. The seed survives only for a record that has no pair.
#
# WHAT THESE TESTS ARE REALLY DEFENDING is the NEGATIVE: that the repo arm never falls back to
# disk. A missing template repo must FAIL the step, because shipping starter bytes where the
# customer expected their own is the trust violation, and it is worse than a failed step precisely
# because it succeeds. Two of the tests below therefore assert on what was NOT read, not on output.
# --------------------------------------------------------------------------- #


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
    assert f.gh.git.tree("main") != _EXPECTED_PUSHED_TREE  # the seed is NOT what shipped
    assert f.gh.read_tree_calls == [("acme", "strands-agentcore", _TEMPLATE_HEAD_SHA)], (
        f.gh.read_tree_calls
    )
    # Still ONE tree write. Reading is not writing: the reads are interleaved before the push and
    # add no writer, so E28B's headline contract survives being extended.
    assert f.gh.pushes == ["main"], f.gh.pushes
    assert all(s.status is StepStatus.DONE for s in repo.steps), [
        (s.key, s.error) for s in repo.steps if s.status is not StepStatus.DONE
    ]


def test_the_fetch_step_LABEL_states_which_repo_and_which_sha_shipped(repo_source_fakes):
    """PROVENANCE IS STATED, NOT GUESSED — and the label is where it is stated.

    Pinned VERBATIM because the frontend renders ``steps[].label`` with no key→label map (pinned
    since E28B/T3), so this string IS the operator-facing copy: there is no other place to change
    it and no translation layer that could rescue a wrong one. An operator asking "which bytes did
    this agent get" reads the answer off the timeline instead of inferring it."""
    f = repo_source_fakes
    repo = _materialized(f)

    step = next(s for s in repo.steps if s.key == "push_template")
    assert step.label == f"Fetch template (strands-agentcore@{_TEMPLATE_HEAD_SHA[:7]})", step.label
    # The pre-28C label named the ACTION and hid the source. It must not survive on this arm.
    assert step.label != "Push template contents"


def test_the_repo_arm_NEVER_reads_the_seed_directory_even_though_one_exists(repo_source_fakes):
    """THE NO-SILENT-FALLBACK PIN, asserted on what was NOT read rather than on the output.

    A seed for ``strands-agentcore`` EXISTS on disk in this fixture (that is the trap: the fallback
    would work), so an implementation that read disk and then overlaid the repo bytes — or read
    disk "for comparison" — would ship the right tree and pass every output assertion. This
    monkeypatches the filesystem harvest itself: any disk read at all on this arm fails the test.

    Why it matters beyond tidiness: an implementation that touches disk on the repo arm has a
    fallback path, and a fallback path is one exception handler away from becoming the silent
    degradation D-C2 forbids."""
    f = repo_source_fakes
    import services.project_service as ps_module

    def _explode(*_a, **_kw):
        raise AssertionError("the repo arm must not read the on-disk seed at all")

    original = ps_module.collect_scaffold_files
    ps_module.collect_scaffold_files = _explode
    try:
        repo = _materialized(f)
    finally:
        ps_module.collect_scaffold_files = original

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
    f.gh.template_repos.clear()  # the record still points at it; the repo is gone.

    repo = _materialized(f)

    failed = [s for s in repo.steps if s.status is StepStatus.FAILED]
    assert [s.key for s in failed] == ["push_template"], [(s.key, s.status) for s in repo.steps]
    assert failed[0].error == (
        "template repo strands-agentcore missing — re-seed or deregister on the Templates page"
    ), failed[0].error
    assert repo.cicd_status == "failed" and repo.status == "failed"
    # NOTHING was pushed. Not the seed, not anything: the seed arm was never entered.
    assert f.gh.pushes == [], f.gh.pushes
    assert "commit_files" not in f.gh.calls, f.gh.calls
    # And no tree read was attempted against a repo ``read_repo`` already said was absent.
    assert f.gh.read_tree_calls == [], f.gh.read_tree_calls


def test_an_EMPTY_template_repo_is_a_failure_not_a_blank_ref_read(repo_source_fakes):
    """T1's edge case, and the one that would corrupt quietly. The repo EXISTS but carries no
    commit, so ``read_repo`` answers a view whose ``head_sha`` is EMPTY.

    Forwarding that "" to ``read_tree`` is the mixed-tree defect with the guard removed — the seam
    refuses a blank ref precisely so nobody reads "whatever HEAD is now" — so materialize must
    never make the call. It is treated as the SAME failure as missing (present with nothing to
    ship is, to a materialize, indistinguishable from absent), and it must not degrade to the seed.

    Pinned on read_tree NOT being called, not merely on the step failing: an implementation that
    called it and let the seam's refusal bubble up would fail the step too, with a message about
    refs that tells the operator nothing about their empty template.

    E28D — the message no longer says "missing" (the missing arm's wording, pinned by the test
    above). An empty repo EXISTS and the operator can see it in the provider UI, so calling it
    missing sent them hunting for a deletion that never happened. Same branch, same
    ``materialize_error`` kind, same two remedies — a different sentence about which fact holds."""
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
    # The two arms are DISTINGUISHABLE, which is the whole point of the split.
    assert "missing" not in failed[0].error, failed[0].error
    assert f.gh.read_tree_calls == [], f"a blank ref was forwarded: {f.gh.read_tree_calls}"
    assert f.gh.pushes == [], f.gh.pushes


def test_a_template_repo_whose_TREE_IS_EMPTY_at_a_real_sha_fails_attributably(repo_source_fakes):
    """A repo that EXISTS, has a real commit, and whose tree reads EMPTY. Distinct from both the
    missing repo and the never-committed one: ``read_repo`` answers a real ``head_sha``, so arm 1
    is entered and ``read_tree`` genuinely returns ``{}``.

    ``commit_files`` would refuse this anyway (an empty tree DELETES the branch's contents), so the
    guard is not about safety — it is about ATTRIBUTION. The provider's refusal names the symptom
    ("refusing to commit an empty file set"), which tells an operator that AGP tried to wipe their
    branch; this names the cause and the sha, which tells them their template repo is empty. That is
    the same distinction the seed arm's "no files on disk" guard exists for, and the same one E28B
    kept deliberately.

    ADDED BY REVIEW (T4 F1). This branch shipped unpinned: deleting the whole 9-line guard left all
    214 tests green. My justification for leaving it untested — "the fake cannot produce it, an
    unknown ref raises first" — was FALSIFIED by execution: seeding a ref whose tree is ``{}`` gives
    a real ``head_sha`` and a non-raising ``read_tree``, which is exactly this shape."""
    f = repo_source_fakes
    f.gh.template_repos.clear()
    f.gh.seed_template_repo("acme", "strands-agentcore", trees={_TEMPLATE_HEAD_SHA: {}})

    repo = _materialized(f)

    failed = [s for s in repo.steps if s.status is StepStatus.FAILED]
    assert [s.key for s in failed] == ["push_template"], [(s.key, s.status) for s in repo.steps]
    # Attributed to the TEMPLATE REPO and its sha — not to a branch AGP was about to empty.
    assert failed[0].error == (
        f"template repo strands-agentcore has no files at {_TEMPLATE_HEAD_SHA[:7]} — nothing to push"
    ), failed[0].error
    # The arm was really entered (so this is not the missing-repo path in disguise) ...
    assert f.gh.read_tree_calls == [("acme", "strands-agentcore", _TEMPLATE_HEAD_SHA)], (
        f.gh.read_tree_calls
    )
    # ... and it still did not fall back to the seed, nor reach the provider with an empty tree.
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
    # head_sha = the LAST ref inserted, so the tip is the ORIGINAL tree and the later push is a
    # ref the implementation must not reach for. (Inserted tip-last deliberately: the trap is an
    # implementation that reads "the newest thing it can see".)
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
    assert f.gh.read_tree_calls == [("acme", "strands-agentcore", _TEMPLATE_HEAD_SHA)], (
        f.gh.read_tree_calls
    )


def test_a_record_with_NO_source_pair_falls_back_to_the_seed_and_SAYS_SO(tenant_service_fakes):
    """ARM 2. A pre-28C record (or an upload pointing outside the org) has no structural source, so
    there is nothing to dereference and the on-disk seed is the honest answer.

    LOUDLY, though — the label names the fallback. A silent seed push is what E28C exists to end,
    and "silent" is the part that made it a defect; a seed push an operator can SEE is a legitimate
    bootstrap. The provider is never asked about a repo AGP has no pointer to."""
    f = tenant_service_fakes
    f.svc._templates = _FakeTemplateRegistry([_template_record()])  # both halves None

    repo = _materialized(f)

    assert f.gh.git.tree("main") == _EXPECTED_PUSHED_TREE
    step = next(s for s in repo.steps if s.key == "push_template")
    assert step.label == f"Fetch template (seed: {TEMPLATE_NAME})", step.label
    # No pointer ⇒ no probe. Asking the provider about a repo AGP cannot name is a wasted call at
    # best and a guess at worst (a guessed org names a repo that may not exist — T2's reason for
    # storing the pair instead of parsing ``source_url``).
    assert f.gh.read_repo_calls == [], f.gh.read_repo_calls
    assert f.gh.read_tree_calls == [], f.gh.read_tree_calls


@pytest.mark.parametrize(
    "source_org,source_repo",
    [("acme", None), (None, "strands-agentcore"), ("", "strands-agentcore"), ("acme", "")],
)
def test_only_BOTH_halves_make_a_record_dereferenceable(
    tenant_service_fakes, source_org, source_repo
):
    """T2's pin, enforced at the CONSUMER. Half a pair is not a pointer: ``read_repo`` takes org
    AND repo positionally, so a half-set record cannot be dereferenced at all.

    Empty strings are included because ``Optional[str]`` admits them and a truthiness check and an
    ``is not None`` check disagree exactly there — the arm must be chosen on USABILITY, not on
    whether the field was assigned."""
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
    """A name with no catalog row at all. There is no pointer, so this is arm 2 — the seed, labeled
    as the seed. It is NOT the missing-repo failure: nothing claimed a repo exists, so nothing is
    inconsistent, and the E28B bootstrap path (materialize a name whose seed ships in the image,
    before any template was ever registered) must keep working."""
    f = tenant_service_fakes
    registry = _FakeTemplateRegistry([])  # empty catalog
    f.svc._templates = registry

    repo = _materialized(f)

    assert f.gh.git.tree("main") == _EXPECTED_PUSHED_TREE
    step = next(s for s in repo.steps if s.key == "push_template")
    assert step.label == f"Fetch template (seed: {TEMPLATE_NAME})", step.label
    assert f.gh.read_repo_calls == [], f.gh.read_repo_calls
    # The catalog WAS consulted, scoped to the PROJECT'S CONNECTION — a lookup that skipped the
    # connection would read another connection's row for the same template name.
    assert registry.get_calls == [("c1", TEMPLATE_NAME)], registry.get_calls


def test_an_UNREADABLE_catalog_fails_the_step_rather_than_degrading_to_the_seed(
    tenant_service_fakes,
):
    """"AGP could not look" is not evidence of absence — the same rule T1 gave ``read_repo``.

    A store fault must NOT be folded into "this record has no source pair", because that would
    ship seed bytes over a dereferenceable template every time DynamoDB hiccupped: an intermittent
    silent downgrade, which is strictly worse than a loud failure an operator retries."""
    f = tenant_service_fakes
    f.svc._templates = _FakeTemplateRegistry(raise_on_get=RuntimeError("ddb unavailable"))

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
    assert f.svc._templates is None  # the fixture wires none

    repo = _materialized(f)

    assert f.gh.git.tree("main") == _EXPECTED_PUSHED_TREE
    step = next(s for s in repo.steps if s.key == "push_template")
    assert step.label == f"Fetch template (seed: {TEMPLATE_NAME})", step.label


def test_a_retried_materialize_re_reads_the_repo_and_converges(repo_source_fakes):
    """Resume safety on the repo arm. ``retry_materialize`` re-derives its inputs from DURABLE
    state (the stash is popped on failure), so the retry must reach the SAME template repo — the
    record is looked up again from the durable ``template_name`` + the project's connection.

    Converges rather than re-pushing: the tree is content-addressed, so re-reading the same sha
    produces the tree already on the branch and no second commit is written. A retry that pushed
    again would fire a second build, which is the duplicate-build regression E28B removed."""
    f = repo_source_fakes
    f.gh.template_repos.clear()  # first attempt fails: repo gone
    repo = _materialized(f)
    assert repo.cicd_status == "failed"

    # The operator re-seeds the template repo, then retries.
    f.gh.seed_template_repo("acme", "strands-agentcore", trees={_TEMPLATE_HEAD_SHA: _REPO_TREE})
    f.svc.retry_materialize(repo.id)
    f.svc.run_materialize(repo.id)
    retried = f.svc.get_repo(repo.id)

    assert all(s.status is StepStatus.DONE for s in retried.steps), [
        (s.key, s.error) for s in retried.steps if s.status is not StepStatus.DONE
    ]
    assert f.gh.git.tree("main") == _REPO_TREE
    assert f.gh.pushes == ["main"], f.gh.pushes  # exactly one push across both attempts
    step = next(s for s in retried.steps if s.key == "push_template")
    assert step.label == f"Fetch template (strands-agentcore@{_TEMPLATE_HEAD_SHA[:7]})", step.label


# --------------------------------------------------------------------------- #
# E28B/T3 — the stage-scoped CI vars stay REPO-LEVEL (the Q1 narrowing).
#
# Deleting the two ``create_environment`` calls and ``set_environment_variables`` removed the only
# writer of ECR_REPOSITORY / AWS_REGION / AWS_ECR_PUSH_ROLE_ARN, and ``set_repo_vars`` used to POP
# exactly those three whenever a tenant service was wired. Left alone, materialize would report
# `ready` while EVERY build failed with no registry, no region and no push role. These tests are
# the fence around that.
# --------------------------------------------------------------------------- #


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
    """``scope=None`` only. A named scope must already exist on the provider, and creating one
    would be exactly the SECOND write to a fresh repository that E28B exists to remove."""
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
    # D-B2b(4) — THE MESSAGE REACHES THE OPERATOR, on the PERSISTED record (not just the log).
    # `run_materialize` used to store `type(err).__name__`, so the operator read "ProjectError":
    # no indication that the fix is a tenant config change, and nothing naming what the tenant
    # actually has. Asserted on CONTENT — the stage names, the missing one, and the tenant — so
    # deleting any part of the message reddens this test.
    hint = failed[0].error
    assert "dev" in hint and "staging" in hint and "prod" in hint, hint
    assert "default" in hint, hint  # the tenant whose config must change
    # And it stays SAFE: the connection token must never ride out on a read model.
    assert "ghp_secret" not in hint
    assert "ghp_secret" not in repo.model_dump_json()
    # No staging credential was written on the way to failing.
    assert "ecr-staging-uri" not in str(f.gh.repo_vars)
    # The template DID land — the failure is scoped to the var write, so a retry resumes there.
    assert f.gh.git.tree("main") == _EXPECTED_PUSHED_TREE


def test_an_arbitrary_exception_never_puts_its_message_on_the_record(tenant_service_fakes):
    """THE FENCE around D-B2b(4). Surfacing `ProjectError`'s message is only safe because it is a
    CURATED literal this module authors; a provider/Graph/boto message can carry a token, a URL or
    a response body, and the step `error` lands on a read model the console renders.

    So an arbitrary exception must still degrade to its TYPE NAME. Verified with a message that
    genuinely contains a credential — if the hint ever widened to `str(err)` for everything, this
    test fails and the leak is caught here rather than in a support ticket."""
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


# --------------------------------------------------------------------------- #
# E31 (review C2 / I5) — THE PER-ORG PUSH ROLE WINS AT STAMPING, IN THE TENANT-WIRED PATH.
#
# THE MISSING TEST THAT LET THE BUG SHIP. The override lived only in the
# `self._tenants is None` arm — and `api/routes/projects.py` wires a TenantService
# UNCONDITIONALLY, so production NEVER took that arm. The only coverage asserted the per-org
# ARN landed on the CONNECTION RECORD (`test_connection_service.py`), and nothing asserted
# what reached the repo's `AWS_ECR_PUSH_ROLE_ARN` in the branch production actually runs.
# That gap is exactly why the design read as "multi-org is already served by the per-org
# roles" when in the wired path it was not.
#
# The concrete failure it hid: org A connects, then org B. B's per-org role
# `<prefix>-ecr-push-B` is created, trusted `repo:B/*:*`, and its ARN stored on B's
# connection — then never stamped, because the tenant stage's `push_role_arn` is the SHARED
# role, whose trust names org A only. Every build in org B died at "Configure AWS
# credentials" with `Not authorized to perform sts:AssumeRoleWithWebIdentity`, while the
# connection read CONNECTED and materialize read `ready`.
#
# These tests are the load-bearing regression net for that. They must fail if the override
# moves back inside the `is None` arm.
# --------------------------------------------------------------------------- #


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
        f.gh.repo_vars["AWS_ECR_PUSH_ROLE_ARN"]
        == "arn:aws:iam::111111111111:role/agp-cp-dev-ecr-push-OrgB"
    )
    # The SHARED role — whose trust names only whichever org connected first — must NOT be
    # what org B's repos are told to assume. This is the assertion that reddens on a revert.
    assert f.gh.repo_vars["AWS_ECR_PUSH_ROLE_ARN"] != "arn:aws:iam::111111111111:role/push-dev"
    # The other two stage keys are UNTOUCHED: a per-org role is a push credential, not a
    # registry, and it is scoped to the same shared agent-images repo the stage names.
    assert f.gh.repo_vars["ECR_REPOSITORY"] == "ecr-dev-uri"
    assert f.gh.repo_vars["AWS_REGION"] == "us-east-1"


def test_a_connection_with_NO_per_org_role_still_gets_the_tenant_stage_value(
    tenant_service_fakes,
):
    """The fallback half of the same precedence, and it is what keeps the shared role
    meaningful: an inert push-role service (unconfigured env) or a pre-E22 connection has no
    per-org ARN, so the tenant stage's value stands exactly as before. Without this, the fix
    would read as "the per-org role is now mandatory"."""
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
    assert (
        f.gh.repo_vars["AWS_ECR_PUSH_ROLE_ARN"] == "arn:aws:iam::111111111111:role/push-OrgB"
    )


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
    """E28/D8's open-stages case, RE-AIMED rather than deleted.

    A ``uat``-only tenant is legitimate (TenantService requires only ONE stage), and the old
    ``set_env_vars`` loop used to raise a bare ``KeyError: 'dev'`` that the step wrapper swallowed
    into an inscrutable ``error="KeyError"``. Under E28B such a tenant still cannot satisfy the
    template's ``environment: dev`` — but the refusal must be DELIBERATE and named, not a KeyError.
    That is the contract the D-B8 test was really protecting, so it survives with a new target."""
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
    # A NAMED, ACTIONABLE refusal — not a bare `KeyError` leaking through the wrapper, and not the
    # equally useless `"ProjectError"` the persisted hint used to be. It must name the stage that is
    # missing and the one the tenant does have.
    assert "dev" in step.error and "uat" in step.error, step.error
    assert len(repo.steps) == 6  # the D-B2/D-C5 timeline contract is untouched by this path


def test_a_three_stage_tenant_wires_only_dev_into_ci(tenant_service_fakes):
    """E28/D8 REGRESSION, re-aimed: a dev+uat+prod tenant must still materialize cleanly, and CI
    must carry ONLY dev's credentials.

    The old test asserted all three stages got environments and vars — the write that no longer
    exists. What still matters is that extra stages neither break materialize nor leak into CI: a
    `uat` push role in the build would let a dev push deploy into the uat account."""
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
    # Neither of the other two stages reached CI, by account id (the value that would matter).
    for absent in ("333333333333", "222222222222", "ecr-uat-uri", "ecr-prod-uri"):
        assert absent not in str(f.gh.repo_vars), f"{absent} leaked into CI vars"


def test_add_repo_keeps_agent_and_connection_repo_level(tenant_service_fakes):
    """AGENT_ID/CONNECTION_ID are stamped last and are never overridable — unchanged by E28B.

    The three assertions that the stage keys were ABSENT from the repo-level set are inverted now
    (they must be PRESENT — see ``test_the_stage_scoped_vars_are_written_repo_level``); that
    inversion is the deliberate consequence of deleting the environment writes."""
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
    svc._tenants = None  # legacy / no tenant service
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
    assert fake_gh.repo_vars  # a single repo-level write happened
    assert fake_gh.repo_vars["ECR_REPOSITORY"] == "123456789012.dkr.ecr.us-east-1.amazonaws.com/agp"
    assert fake_gh.repo_vars["AWS_REGION"] == "us-east-1"
    assert fake_gh.scoped_writes == []  # nothing scoped, with or without a tenant service


# --------------------------------------------------------------------------- #
# add_repo — resolve/validate guards
# --------------------------------------------------------------------------- #


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


def test_add_repo_rejects_non_strands(project_service_fakes):
    svc = project_service_fakes
    _make_project(svc)
    with pytest.raises(ValueError, match="framework"):
        svc.add_repo(
            project_id="proj-1",
            name="p1",
            template_name=TEMPLATE_NAME,
            agent_config={"agent_name": "a", "framework": "langgraph"},
            created_by="op@x",
            principal=_principal(),
        )
    # A bad config is rejected before any side effect.
    svc._registry.create.assert_not_called()
    svc._rollout.create_repo.assert_not_called()


# --------------------------------------------------------------------------- #
# add_repo — happy path
# --------------------------------------------------------------------------- #


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

    # Identity minted before the runtime exists (agent_arn is None).
    svc._identity.provision_identity.assert_called_once()
    assert svc._identity.provision_identity.call_args.args[0].agent_arn is None
    # Repo created in the connection's org with the connection token.
    cargs = svc._rollout.create_repo.call_args
    assert cargs.args[0] == "acme"  # org
    assert cargs.args[1] == "p1"  # new repo name
    assert cargs.kwargs["token"] == "ghp_secret"  # forwarded to the write client
    svc._rollout.set_ci_vars.assert_called_once()
    set_vars = svc._rollout.set_ci_vars.call_args.args[2]
    assert set_vars["AGENT_ID"] == "agent-1"

    assert repo.project_id == "proj-1"
    assert repo.agent_id == "agent-1"
    # E27/T5: the agent is stamped with the SAME project the repo was created under —
    # a server-side keyword on registry.create (not an AgentCreate field), so it is
    # pinned off call_args.kwargs rather than the AgentCreate positional.
    assert svc._registry.create.call_args.kwargs["project_id"] == repo.project_id
    assert repo.template_name == "strands-agentcore"
    assert repo.repo_url == "https://github.com/acme/p1"
    # E25C/T2 + E28A/T5: terminal success — finalize flips BOTH to "ready". This line used to
    # assert "provisioning" two lines above the "ready" below, which documented the bug rather
    # than a contract: the record claimed to be still provisioning while its CI badge said ready.
    assert repo.status == "ready"
    assert repo.cicd_status == "ready"
    # All background steps completed.
    assert all(s.status == "done" for s in repo.steps)
    assert len(repo.steps) == 6  # D-B2's five + D-C5's provision_langfuse
    # Token never leaks onto the read model.
    assert "ghp_secret" not in repo.model_dump_json()
    # Persisted in the repository partition.
    assert [r.id for r in svc.list_repositories()] == [repo.id]


def test_add_repo_passes_model_id_from_agent_config(project_service_fakes):
    """The pre-registered Agent carries model_id from agent.config.json (E21), so it
    reaches the registry envelope the runtime build reads for its tfvars."""
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
    """REPLACES ``test_add_repo_commits_agent_config_content``.

    That test asserted the operator's config was committed as ``agent.config.json``. D-B2 deletes
    that file: the runtime never read it, and four findings existed only to keep the copy on the
    right branch. The CONTRACT it stood for — the operator's config reaching the runtime — is
    pinned by ``test_the_operators_config_still_reaches_the_deployed_container`` and by
    ``test_add_repo_passes_model_id_from_agent_config``, both of which follow the registry-record
    path that actually feeds the buildspec. What is asserted here is the negative."""
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


# --------------------------------------------------------------------------- #
# add_repo — failure envelope
# --------------------------------------------------------------------------- #


def test_add_repo_materialize_failure_persists_failed_repo(project_service_fakes):
    """E25C/T2: a materialize failure now surfaces in the BACKGROUND run_materialize (after
    the 202) — it marks the failing step + the record 'failed' and NEVER raises. add_repo
    itself only persists the pending record (no exception)."""
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
    svc.run_materialize(repo.id)  # must NOT raise
    # The identity was minted before the failure (reusable via /reprovision).
    svc._identity.provision_identity.assert_called_once()
    # A failed Repository stays persisted (queryable), not swallowed.
    repos = svc.list_repositories()
    assert len(repos) == 1
    assert repos[0].status == "failed"
    assert repos[0].cicd_status == "failed"
    assert repos[0].agent_id == "agent-1"
    assert repos[0].repo_url is None
    # The failing step is 'create_repo'; the mint_identity before it is 'done'.
    by_key = {s.key: s.status for s in repos[0].steps}
    assert by_key["mint_identity"] == "done"
    assert by_key["create_repo"] == "failed"


# --------------------------------------------------------------------------- #
# get_project / list_repositories
# --------------------------------------------------------------------------- #


def test_get_project_returns_detail_with_repos(project_service_fakes):
    svc = project_service_fakes
    _make_project(svc)
    r1 = svc.add_repo(
        project_id="proj-1",
        name="p1",
        template_name=TEMPLATE_NAME,
        agent_config=VALID_AGENT_CONFIG,
        created_by="op@x",
        principal=_principal(),
    )

    detail = svc.get_project("proj-1")
    assert detail is not None
    assert detail.project.id == "proj-1"
    assert [r.id for r in detail.repositories] == [r1.id]


def test_get_project_unknown_returns_none(project_service_fakes):
    assert project_service_fakes.get_project("ghost") is None


def test_list_repositories_flat(project_service_fakes):
    svc = project_service_fakes
    _make_project(svc)
    r1 = svc.add_repo(
        project_id="proj-1",
        name="p1",
        template_name=TEMPLATE_NAME,
        agent_config=VALID_AGENT_CONFIG,
        created_by="op@x",
        principal=_principal(),
    )
    r2 = svc.add_repo(
        project_id="proj-1",
        name="p2",
        template_name=TEMPLATE_NAME,
        agent_config=VALID_AGENT_CONFIG,
        created_by="op@x",
        principal=_principal(),
    )
    ids = {r.id for r in svc.list_repositories()}
    assert ids == {r1.id, r2.id}


# --------------------------------------------------------------------------- #
# E24/T6 — tenant_id: persisted on create, inherited by the repo's agent,
# legacy stored records hydrate as "default"
# --------------------------------------------------------------------------- #


def test_create_project_persists_tenant_id(project_service_fakes):
    svc = project_service_fakes
    p = _make_project(svc, tenant_id="ten-9")
    assert p.tenant_id == "ten-9"
    # Round-trips through the read path unchanged.
    detail = svc.get_project("proj-1")
    assert detail.project.tenant_id == "ten-9"
    assert svc.list_projects()[0].tenant_id == "ten-9"


def test_add_repo_agent_inherits_project_tenant_id(project_service_fakes):
    """The pre-registered agent carries the PROJECT's actual tenant_id — the T4
    ``or "default"`` compatibility shim is gone (repos inherit through the project)."""
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


class _LegacyProjectTable:
    """A fake DDB table returning a stored PRE-E24 project item (no tenant_id key)."""

    def __init__(self):
        self._item = {
            "pk": "project",
            "sk": "legacy-1",
            "id": "legacy-1",
            "name": "legacy",
            "connection_id": "c1",
            "description": "",
            "created_by": "op@x",
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
        }

    def get_item(self, **kwargs):
        if kwargs.get("Key", {}).get("pk") == "project":
            return {"Item": dict(self._item)}
        return {}

    def query(self, **kwargs):
        # Only the ``project`` partition holds the legacy item; the ``repository``
        # partition scan (get_project loads repos too) must come back empty.
        expr = kwargs.get("KeyConditionExpression")
        pk = expr._values[1] if expr is not None else None
        if pk == "project":
            return {"Items": [dict(self._item)]}
        return {"Items": []}


def _ddb_svc_with_legacy_project(project_service_fakes):
    # Force the DDB path (non-empty table_name) then swap in the legacy-item table
    # (mirrors test_enrollment_service's _FailingTable idiom).
    svc = project_service_fakes
    svc.table_name = "projects"
    svc._table = _LegacyProjectTable()
    assert svc._has_ddb is True
    return svc


def test_legacy_stored_project_hydrates_tenant_id_default(project_service_fakes):
    """A pre-E24 stored record WITHOUT tenant_id hydrates as tenant_id="default" in
    the service read path (matches Task 9's seed) — get AND list."""
    svc = _ddb_svc_with_legacy_project(project_service_fakes)

    detail = svc.get_project("legacy-1")
    assert detail is not None
    assert detail.project.tenant_id == "default"

    projects = svc.list_projects()
    assert [p.id for p in projects] == ["legacy-1"]
    assert projects[0].tenant_id == "default"


def test_stored_project_with_tenant_id_keeps_it(project_service_fakes):
    """Hydration only fills the MISSING key — a stored tenant_id is never overwritten."""
    svc = _ddb_svc_with_legacy_project(project_service_fakes)
    svc._table._item["tenant_id"] = "ten-2"

    detail = svc.get_project("legacy-1")
    assert detail.project.tenant_id == "ten-2"
    assert svc.list_projects()[0].tenant_id == "ten-2"


# --------------------------------------------------------------------------- #
# resilient reads — a malformed persisted row must not 500 the whole list
# --------------------------------------------------------------------------- #


class _FakeTable:
    """A minimal DDB Table double: serves raw ``Items`` per partition (``pk``) for
    ``query`` and a single ``Item`` for ``get_item`` — enough to drive the DDB read
    branch of ProjectService against hand-crafted (incl. malformed) rows."""

    def __init__(self, items):
        self._items = items  # list of raw dict rows, each with a "pk"/"sk"

    def query(self, **kwargs):
        expr = kwargs["KeyConditionExpression"]
        pk = expr._values[1]  # Key("pk").eq(<pk>) → the compared value
        return {"Items": [i for i in self._items if i.get("pk") == pk]}

    def get_item(self, Key):  # noqa: N803 — boto3 param name
        for i in self._items:
            if i.get("pk") == Key["pk"] and i.get("sk") == Key["sk"]:
                return {"Item": i}
        return {}


def _ddb_service(project_service_fakes, items):
    """Flip the fixture's service into DDB mode with a fake table of raw rows."""
    svc = project_service_fakes
    svc.table_name = "fake-table"
    svc._table = _FakeTable(items)
    return svc


_VALID_REPO_ROW = {
    "pk": "repository",
    "sk": "repo-ok",
    "id": "repo-ok",
    "project_id": "proj-1",
    "name": "good",
    "repo_url": "https://github.com/acme/good",
    "agent_id": "agent-1",
    "template_name": "strands-agentcore",
    "cicd_status": "provisioning",
    "status": "provisioning",
    "created_by": "op@x",
    "created_at": "2026-07-02T00:00:00+00:00",
    "updated_at": "2026-07-02T00:00:00+00:00",
}

# Same shape as the valid row but MISSING the required ``template_name`` — a pre-rename /
# partial-write row that makes Repository.model_validate raise.
_BAD_REPO_ROW = {**{k: v for k, v in _VALID_REPO_ROW.items() if k != "template_name"},
                 "sk": "repo-bad", "id": "repo-bad", "name": "bad"}


def test_list_repositories_skips_malformed_row(project_service_fakes):
    svc = _ddb_service(project_service_fakes, [_VALID_REPO_ROW, _BAD_REPO_ROW])

    repos = svc.list_repositories()

    # The one malformed row is skipped, not raised — only the valid repo comes back.
    assert [r.id for r in repos] == ["repo-ok"]


def test_get_project_repositories_skips_malformed_row(project_service_fakes):
    project_row = {
        "pk": "project",
        "sk": "proj-1",
        "id": "proj-1",
        "name": "p1",
        "connection_id": "c1",
        "description": "the first project",
        "created_by": "op@x",
        "created_at": "2026-07-02T00:00:00+00:00",
        "updated_at": "2026-07-02T00:00:00+00:00",
    }
    svc = _ddb_service(
        project_service_fakes, [project_row, _VALID_REPO_ROW, _BAD_REPO_ROW]
    )

    detail = svc.get_project("proj-1")

    assert detail is not None
    # _load_repos_for → _load_all_repos: the bad row is skipped, not fatal.
    assert [r.id for r in detail.repositories] == ["repo-ok"]


# --------------------------------------------------------------------------- #
# E27/T7 — a backend save must NEVER clobber the attributes CodeBuild owns
# (last_dev_image_tag, and cicd_status unless the caller opts in). The buildspec
# writes them via a targeted UpdateItem; a whole-item put_item would wipe them.
# --------------------------------------------------------------------------- #


class _UpdatingFakeTable(_FakeTable):
    """``_FakeTable`` plus REAL ``update_item`` merge semantics: it applies the
    ``SET #n = :v`` expression onto the stored row so attributes the expression does not
    name SURVIVE — exactly how DynamoDB behaves, and the whole point of the T7 fix."""

    def update_item(self, **kwargs):  # noqa: N803 — boto3 param names
        key = kwargs["Key"]
        names = kwargs["ExpressionAttributeNames"]
        values = kwargs["ExpressionAttributeValues"]
        row = next(
            (i for i in self._items if i.get("pk") == key["pk"] and i.get("sk") == key["sk"]),
            None,
        )
        if row is None:
            row = {"pk": key["pk"], "sk": key["sk"]}
            self._items.append(row)
        for assignment in kwargs["UpdateExpression"][len("SET ") :].split(", "):
            name_ph, value_ph = (p.strip() for p in assignment.split("="))
            row[names[name_ph]] = values[value_ph]


def _codebuild_wrote(table, repo_id, tag="agent7-abc123", status="deployed"):
    """Simulate the buildspec's out-of-band targeted UpdateItem on the live row."""
    row = next(i for i in table._items if i.get("sk") == repo_id)
    row["last_dev_image_tag"] = tag
    row["cicd_status"] = status


def _ddb_svc_with_live_repo(project_service_fakes):
    svc = project_service_fakes
    svc.table_name = "fake-table"
    svc._table = _UpdatingFakeTable([dict(_VALID_REPO_ROW)])
    assert svc._has_ddb is True
    return svc


def test_save_repo_after_codebuild_write_keeps_the_tag_and_status(project_service_fakes):
    """The exact I1 interleaving: backend READS the repo → CodeBuild writes
    last_dev_image_tag + cicd_status → backend SAVES the stale record. Both
    CodeBuild-owned attributes must survive."""
    svc = _ddb_svc_with_live_repo(project_service_fakes)

    stale = svc._get_repo("repo-ok")  # read BEFORE CodeBuild's write
    assert stale.last_dev_image_tag is None
    _codebuild_wrote(svc._table, "repo-ok")

    stale.repo_url = "https://github.com/acme/renamed"
    svc._save_repo(stale)  # the save that used to clobber

    after = svc._get_repo("repo-ok")
    assert after.last_dev_image_tag == "agent7-abc123"
    assert after.cicd_status == "deployed"
    assert after.repo_url == "https://github.com/acme/renamed"  # the save still applied


def test_save_repo_step_after_codebuild_write_keeps_the_tag(project_service_fakes):
    """`_save_repo_step` (the long-lived materialize orchestration — one of the two
    plausible clobber windows) must not destroy a concurrently-written tag."""
    svc = _ddb_svc_with_live_repo(project_service_fakes)
    svc._get_repo("repo-ok")
    _codebuild_wrote(svc._table, "repo-ok")

    svc._save_repo_step("repo-ok", "finalize", StepStatus.DONE)

    after = svc._get_repo("repo-ok")
    assert after.last_dev_image_tag == "agent7-abc123"
    assert after.cicd_status == "deployed"
    assert [s.status for s in after.steps if s.key == "finalize"] == [StepStatus.DONE]


def test_retry_materialize_keeps_the_tag_but_does_reset_cicd_status(project_service_fakes):
    """`retry_materialize` is the other window. It legitimately OWNS the cicd_status
    reset (opt-in), but must still never touch the CodeBuild-exclusive tag."""
    svc = _ddb_svc_with_live_repo(project_service_fakes)
    svc._table._items.append(
        {
            "pk": "project",
            "sk": "proj-1",
            "id": "proj-1",
            "name": "p1",
            "connection_id": "c1",
            "tenant_id": "default",
            "description": "d",
            "created_by": "op@x",
            "created_at": "2026-07-02T00:00:00+00:00",
            "updated_at": "2026-07-02T00:00:00+00:00",
        }
    )
    _codebuild_wrote(svc._table, "repo-ok", status="deployed")

    svc.retry_materialize("repo-ok")

    after = svc._get_repo("repo-ok")
    assert after.last_dev_image_tag == "agent7-abc123"  # never the backend's to write
    assert after.cicd_status == "provisioning"  # the opt-in transition DID apply


def test_local_save_repo_also_preserves_codebuild_owned_fields(project_service_fakes):
    """The in-memory fallback mirrors the DDB semantics, so service tests
    (``table_name=""``) exercise the same protection."""
    svc = project_service_fakes
    svc._save_repo(
        Repository(
            id="repo-local",
            project_id="proj-1",
            name="my-agent",
            agent_id="agent-1",
            template_name=TEMPLATE_NAME,
            cicd_status="provisioning",
            status="provisioning",
            created_by="op@x",
            created_at="2026-07-02T00:00:00+00:00",
            updated_at="2026-07-02T00:00:00+00:00",
        ),
        include_cicd_status=True,
    )
    stale = svc._get_repo("repo-local")
    # CodeBuild's out-of-band write, on the stored record.
    svc._local_repos["repo-local"].last_dev_image_tag = "agent7-abc123"
    svc._local_repos["repo-local"].cicd_status = "deployed"

    svc._save_repo(stale)

    after = svc._get_repo("repo-local")
    assert after.last_dev_image_tag == "agent7-abc123"
    assert after.cicd_status == "deployed"


# --------------------------------------------------------------------------- #
# E28A/T5 — `repo.status` must have a SUCCESS value, and the cycle must close.
#
# Before this, `status` had exactly two writers: "provisioning" (create + retry) and
# "failed". NOTHING wrote a success value, so a fully materialized healthy repo read
# "provisioning" forever beside a Complete timeline — and `_finalize_repo`'s docstring
# documented that as intentional, which is what made it a CONTRACT change rather than a
# bug fix. The three tests below pin the whole three-value cycle, because pinning only
# the `ready` write would leave the reset and the failure value free to drift.
# --------------------------------------------------------------------------- #


def test_finalize_advances_status_to_ready(project_service_fakes):
    """The success value the field never had. Asserted against a RE-READ, never the returned
    object: `_finalize_repo` mutates a record it read itself, so the returned/ in-memory copy
    would say "ready" even if the save dropped it — T7's clobber class, and the FE polls the
    STORED record."""
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
    assert svc.get_repo(repo.id).status == "provisioning"  # in flight, before finalize

    svc.run_materialize(repo.id)

    stored = svc.get_repo(repo.id)
    assert stored.status == "ready"
    # …alongside the cicd_status flip finalize always did — the two now agree instead of one
    # reading terminal-success while the other reads in-flight.
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
    svc.run_materialize(repo.id)  # must NOT raise

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

    # Heal the fault ON THE METHOD THE FAILURE WAS INJECTED ON. E28B/T7: this line used to clear
    # `generate_from_template`, which is not the injected method and does not exist any more — on a
    # MagicMock that silently auto-created the attribute and healed NOTHING, so the retry below was
    # asserted against a service still failing at `create_repo`. The reset only reads as a no-op
    # because this test asserts `status == "provisioning"`, which `retry_materialize` writes before
    # the run; a test that went on to assert a SUCCESSFUL retry would have been quietly wrong.
    svc._rollout.create_repo.side_effect = None
    svc.retry_materialize(repo.id)

    assert svc.get_repo(repo.id).status == "provisioning"


def test_finalize_does_not_rewrite_the_codebuild_owned_dev_tag(project_service_fakes):
    """The skip-set regression the new write could have introduced. `status` is in NO skip set
    (which is why T5 needed no signature change), but `_finalize_repo` saves the whole record —
    so a tag CodeBuild wrote out-of-band during the materialize must still survive the save
    that now also advances `status`."""
    svc = _ddb_svc_with_live_repo(project_service_fakes)
    svc._get_repo("repo-ok")  # the backend's read, BEFORE CodeBuild's write
    _codebuild_wrote(svc._table, "repo-ok")

    svc._finalize_repo("repo-ok", "https://github.com/acme/good")

    after = svc._get_repo("repo-ok")
    assert after.status == "ready"                       # the T5 write DID apply
    assert after.last_dev_image_tag == "agent7-abc123"    # …without touching CodeBuild's


# --------------------------------------------------------------------------- #
# delete_repo — the E23/T4 teardown cascade
# --------------------------------------------------------------------------- #

P = "proj-1"
R = "repo-1"
AGENT_ID = "agent-1"
# E28A/T2: the registry record's `name` — DISTINCT from AGENT_ID on purpose (the exec-role
# name derives from the name, not the id, and a shared literal would hide a mix-up).
AGENT_NAME = "my_platform_agent"
AGENT_ARN = "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/agent-1"


class _FakeDeleteRegistry:
    """Registry double for the teardown cascade — serves an Agent and captures deletes."""

    def __init__(self, agent):
        self.get_return = agent
        self.deleted = None
        self.delete_raises = None

    def get(self, agent_id):
        return self.get_return

    def delete(self, agent_id):
        if self.delete_raises:
            raise self.delete_raises
        self.deleted = agent_id


class _FakeDeleteIdentity:
    """Identity double — ``delete_runtime`` is sync; ``delete_identity`` is async (driven
    via the service's asyncio.run guard)."""

    def __init__(self):
        # E28A/T1: a LIST, not a scalar. Since T1b stage-scopes the runtime resource names an
        # agent owns one runtime PER STAGE, so the cascade must delete N of them — and a fake
        # that overwrites one field cannot tell "deleted all 3" from "deleted the last of 3".
        # A fake more generous than reality is the defect class that recurred through E28
        # (a flat commit_paths list that made every tree assertion pass against a broken
        # implementation); this records EVERY call, in order, so the count is assertable.
        self.deleted_runtime_arns = []
        self.deleted_identity = False
        self.delete_runtime_raises = None
        self.delete_identity_raises = None
        # E23/T11 reachability probe (READ-ONLY).
        self.runtime_exists_return = True
        self.runtime_exists_raises = None
        # E36/T8: WHICH control client each call was handed — None means "the service's own
        # ambient client". Recorded per call (a list, like the ARNs) because the whole point
        # of the cross-account seam is that a per-stage runtime is addressed by a per-stage
        # client; a fake that only remembered the last one could not tell N assumes from one
        # ambient client reused N times.
        self.delete_runtime_clients = []
        self.exists_clients = []
        # E36/T8 fix 1: the ARN of every probe, in order, so `exists_clients` can be zipped
        # against it. Without the pairing, "each stage probed under SOME assumed client" is
        # assertable but "each stage probed under ITS OWN stage's client" is not — and a
        # tenant-keyed (rather than stage-keyed) client cache passes the first while issuing
        # prod's DeleteAgentRuntime under dev's credentials.
        self.probed_runtime_arns = []

    @property
    def deleted_runtime_arn(self):
        """The LAST ARN deleted, or None. Kept so the pre-E28A single-runtime assertions
        still read naturally; anything asserting on the FAN-OUT must use the list."""
        return self.deleted_runtime_arns[-1] if self.deleted_runtime_arns else None

    def delete_runtime(self, agent_arn, *, control_client=None):
        self.delete_runtime_clients.append(control_client)
        if self.delete_runtime_raises:
            raise self.delete_runtime_raises
        # The real service PROBES first and returns WITHOUT deleting when the runtime is
        # definitively gone (E23/T11). Modelled here because the cross-account defect lives
        # in the PROBE, not in the delete: an ambient probe answers NotFound about an account
        # that never held the runtime, and the cascade read that as an idempotent success. A
        # probe that RAISES is AMBIGUOUS and falls through to attempt the delete, exactly as
        # the service does — inferring "gone" from an AccessDenied would orphan a live runtime.
        try:
            gone = not self.runtime_exists(agent_arn, control_client=control_client)
        except Exception:
            gone = False
        if gone:
            return
        self.deleted_runtime_arns.append(agent_arn)

    async def delete_identity(self, agent):
        if self.delete_identity_raises:
            raise self.delete_identity_raises
        self.deleted_identity = True

    def runtime_exists(self, agent_arn, *, control_client=None):
        self.exists_clients.append(control_client)
        self.probed_runtime_arns.append(agent_arn)
        if self.runtime_exists_raises:
            raise self.runtime_exists_raises
        return bool(agent_arn) and self.runtime_exists_return


class _FakeDeleteGitHub:
    def __init__(self):
        self.delete_repo_raises = None
        self.deleted = None
        self.repo_exists_return = True
        self.repo_exists_raises = None

    def delete_repo(self, org, repo, token, base_url=None):
        if self.delete_repo_raises:
            raise self.delete_repo_raises
        self.deleted = (org, repo)

    def repo_exists(self, org, repo, token, base_url=None):
        if self.repo_exists_raises:
            raise self.repo_exists_raises
        return self.repo_exists_return


class _FakeEcr:
    def __init__(self):
        self.delete_images_raises = None
        self.deleted_agent_id = None
        self.count_images_return = 1
        self.count_images_raises = None

    def delete_images(self, agent_id):
        if self.delete_images_raises:
            raise self.delete_images_raises
        self.deleted_agent_id = agent_id
        return 1

    def count_images(self, agent_id):
        if self.count_images_raises:
            raise self.count_images_raises
        return self.count_images_return


class _FakeS3:
    """Capture double for the runtime TF-state teardown.

    ``keys`` is the stored inventory the paginated listing serves; ``deleted_keys`` records
    every object the cascade removed. Since E28/T2 the state key is stage-scoped, so ONE
    agent owns N objects and the teardown lists-then-deletes rather than deleting a single
    known key."""

    def __init__(self):
        self.keys = [f"agentcore-runtime/{AGENT_ID}/dev/terraform.tfstate"]
        self.deleted_keys = []
        self.listed_prefix = None
        self.list_calls = 0
        self.raises = None
        # Fix-1: which keys blow up on delete, so per-key tolerance is testable.
        self.delete_raises_for = {}
        # Fix-1: when True the listing always returns another continuation token — the
        # "store keeps handing back a token" case the page ceiling exists for.
        self.endless_pages = False

    def list_objects_v2(self, Bucket, Prefix, ContinuationToken=None):  # noqa: N803
        if self.raises:
            raise self.raises
        self.listed_prefix = Prefix
        self.list_calls += 1
        if self.endless_pages:
            return {"Contents": [{"Key": f"{Prefix}p{self.list_calls}/terraform.tfstate"}],
                    "NextContinuationToken": f"tok-{self.list_calls}"}
        matched = [k for k in self.keys if k.startswith(Prefix)]
        return {"Contents": [{"Key": k} for k in matched]} if matched else {}

    def delete_object(self, Bucket, Key):  # noqa: N803 — boto3 param names
        if self.raises:
            raise self.raises
        if Key in self.delete_raises_for:
            raise self.delete_raises_for[Key]
        self.deleted_keys.append(Key)


def _seed_repo(svc):
    """Persist a project + a materialized repo into the local stores for the cascade."""
    svc._save_project(
        Project(
            id=P,
            name="p1",
            connection_id="c1",
            tenant_id="default",
            description="d",
            created_by="op@x",
            created_at="2026-07-02T00:00:00+00:00",
            updated_at="2026-07-02T00:00:00+00:00",
        )
    )
    svc._save_repo(
        Repository(
            id=R,
            project_id=P,
            name="my-agent",
            repo_url="https://github.com/acme/my-agent",
            agent_id=AGENT_ID,
            template_name=TEMPLATE_NAME,
            cicd_status="provisioning",
            status="provisioning",
            created_by="op@x",
            created_at="2026-07-02T00:00:00+00:00",
            updated_at="2026-07-02T00:00:00+00:00",
        )
    )


@pytest.fixture
def delete_fakes():
    """A ProjectService wired with teardown fakes + a seeded project/repo."""
    agent = SimpleNamespace(
        id=AGENT_ID,
        # E28A/T2: the registry record's ``name`` — the SAME value the buildspec feeds the
        # runtime module as `agent_name`, hence the source of the exec-role name reclaimed
        # by the teardown. Deliberately UNLIKE `id`, so a test asserting on the role name
        # cannot pass by accidentally reading the id.
        name=AGENT_NAME,
        agent_arn=AGENT_ARN,
        # E28A/T1: the DEFAULT fixture agent is a LEGACY record — scalar only, no map. That is
        # what every agent in the registry looks like today, so the whole pre-existing cascade
        # suite keeps asserting the legacy path. The map-bearing case is opted into per-test.
        agent_arns={},
        entra_app_id="app-1",
        entra_sp_id="sp-1",
        # E26/T7: the C1 Langfuse join on the envelope the langfuse teardown step reads.
        langfuse_project_id="clx-proj-1",
        langfuse_key_secret_name="langfuse-agent-agent-1-keys",
    )
    registry = _FakeDeleteRegistry(agent)
    identity = _FakeDeleteIdentity()
    github = _FakeDeleteGitHub()
    ecr = _FakeEcr()
    s3 = _FakeS3()
    # E26/T7: the injected C2 provisioner. Its idempotent ``delete_agent_project`` is the
    # langfuse teardown step; a MagicMock captures the call (default: succeeds, no raise).
    langfuse = MagicMock(name="LangfuseProvisioningService")

    conn = MagicMock()
    conn.get_connection.return_value = SimpleNamespace(org="acme", base_url=None)
    conn.get_bearer_token.return_value = "ghp_secret"

    svc = ProjectService(
        table_name="",
        registry=registry,
        identity=identity,
        connection_service=conn,
        github_repo_service=github,
        ecr_image_service=ecr,
        langfuse_provisioning=langfuse,
        runtime_state_bucket="agp-tf-state",
        now=lambda: FIXED,
    )
    # Override the lazily-built boto3 s3 client with the capture double.
    svc._s3 = s3
    # E28A/T2: the IAM client the exec-role reclaim drives. A MagicMock (house idiom — NOT
    # moto), so ORDER is assertable off ``mock_calls``: the inline policy must be deleted
    # before the role or IAM refuses with DeleteConflict.
    iam = MagicMock(name="iam")
    svc._iam = iam
    _seed_repo(svc)
    return SimpleNamespace(
        svc=svc,
        registry=registry,
        identity=identity,
        github=github,
        ecr=ecr,
        s3=s3,
        iam=iam,
        langfuse=langfuse,
        agent=agent,
    )


def test_delete_repo_full_cascade_order_and_removes_record(delete_fakes):
    f = delete_fakes
    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    assert [i.item for i in res.items if i.outcome == "deleted"] == [
        "github",
        "image",
        "runtime",
        # E28C/T5: the IAM exec-role reclaim is now a REPORTED line-item of its own, ordered
        # immediately after the runtime step it rides inside. It is separate because it must be
        # able to say `failed` WITHOUT flipping the runtime item — the role and the runtime are
        # different artifacts with different consequences, and folding them into one row is what
        # let a denied reclaim read as a clean teardown.
        "exec_role",
        "identity",
        # E26/T7: the Langfuse teardown is now a REPORTED line-item, ordered with
        # identity (the agent's own resource) and before the final record delete.
        "langfuse",
        "record",
    ]
    assert res.record_removed is True
    assert f.registry.deleted == AGENT_ID  # registry entry gone
    assert f.svc._get_repo(R) is None  # row gone
    assert f.identity.deleted_runtime_arn == AGENT_ARN
    assert f.identity.deleted_identity is True
    assert f.ecr.deleted_agent_id == AGENT_ID


def test_delete_repo_keeps_record_when_a_selected_step_fails(delete_fakes):
    f = delete_fakes
    f.github.delete_repo_raises = GitHubRepoError("boom")
    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    assert next(i for i in res.items if i.item == "github").outcome == "failed"
    # A safe reason — never the underlying token/body.
    assert "boom" not in (next(i for i in res.items if i.item == "github").reason or "")
    assert next(i for i in res.items if i.item == "record").outcome == "skipped"
    assert res.record_removed is False
    assert f.svc._get_repo(R) is not None  # row kept for retry


def test_delete_repo_respects_unchecked_items(delete_fakes):
    f = delete_fakes
    sel = RepoDeleteSelection(
        github=False, image=False, runtime=False, identity=False, record=True
    )
    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=sel)

    outcomes = {i.item: i.outcome for i in res.items}
    assert outcomes["github"] == "skipped"
    assert outcomes["image"] == "skipped"
    assert outcomes["runtime"] == "skipped"
    assert outcomes["identity"] == "skipped"
    # All others skipped (not failed) → record still deletes.
    assert res.record_removed is True
    assert f.svc._get_repo(R) is None


def test_delete_repo_record_unchecked_never_removes_row(delete_fakes):
    f = delete_fakes
    sel = RepoDeleteSelection(record=False)
    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=sel)

    assert res.record_removed is False
    assert f.svc._get_repo(R) is not None
    assert next(i for i in res.items if i.item == "record").outcome == "skipped"


def test_delete_repo_idempotent_when_agent_already_gone(delete_fakes):
    f = delete_fakes
    f.registry.get_return = None  # agent record already deleted
    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    # runtime + identity treated as already-done successes; github + image still run.
    assert next(i for i in res.items if i.item == "runtime").outcome == "deleted"
    assert next(i for i in res.items if i.item == "identity").outcome == "deleted"
    assert res.record_removed is True


def test_delete_repo_unknown_repo_raises_not_found(delete_fakes):
    f = delete_fakes
    with pytest.raises(ProjectError) as e:
        f.svc.delete_repo(
            project_id=P, repo_id="nope", selection=RepoDeleteSelection()
        )
    assert e.value.kind == "not_found"


def test_delete_repo_wrong_project_raises_not_found(delete_fakes):
    f = delete_fakes
    with pytest.raises(ProjectError) as e:
        f.svc.delete_repo(
            project_id="other", repo_id=R, selection=RepoDeleteSelection()
        )
    assert e.value.kind == "not_found"


def test_delete_repo_deletes_tf_state_object(delete_fakes):
    f = delete_fakes
    f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())
    # E28/T2: the key is stage-scoped, so the teardown reclaims the agent's whole prefix.
    assert f.s3.deleted_keys == [f"agentcore-runtime/{AGENT_ID}/dev/terraform.tfstate"]


def test_delete_repo_state_delete_failure_does_not_fail_runtime(delete_fakes):
    f = delete_fakes
    f.s3.raises = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "no"}}, "DeleteObject"
    )
    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())
    # The runtime itself is gone; a state-object delete failure does NOT flip it to failed.
    assert next(i for i in res.items if i.item == "runtime").outcome == "deleted"
    assert res.record_removed is True


def test_delete_repo_record_delete_failure_keeps_row(delete_fakes):
    f = delete_fakes
    # Every other selected step succeeds, but the registry delete inside the record step
    # raises — the record item must be "failed", record_removed False, and the row kept.
    f.registry.delete_raises = RuntimeError("ddb down")
    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    assert next(i for i in res.items if i.item == "record").outcome == "failed"
    assert res.record_removed is False
    assert f.svc._get_repo(R) is not None  # row still exists


def test_delete_repo_github_skipped_when_connection_missing(delete_fakes):
    f = delete_fakes
    # The referenced connection was disconnected — the GitHub repo is unreachable for good.
    f.svc._conn.get_connection.side_effect = ConnectionError(
        "Unknown connection", kind="not_found"
    )
    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    github = next(i for i in res.items if i.item == "github")
    assert github.outcome == "skipped"
    assert "connection removed" in (github.reason or "")
    # A skipped github step does NOT block record removal — the rest proceeds.
    assert next(i for i in res.items if i.item == "record").outcome == "deleted"
    assert res.record_removed is True
    assert f.svc._get_repo(R) is None  # row gone


def test_delete_repo_github_still_fails_on_other_connection_error(delete_fakes):
    f = delete_fakes
    # A secret/token error is a real, retryable problem — NOT a removed connection.
    f.svc._conn.get_connection.side_effect = ConnectionError("boom", kind="secret_error")
    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    assert next(i for i in res.items if i.item == "github").outcome == "failed"
    assert res.record_removed is False
    assert f.svc._get_repo(R) is not None  # row kept for retry


# --------------------------------------------------------------------------- #
# Langfuse teardown (E26/T7) — the cascade also tears down the agent's Langfuse
# project + SM secret via the idempotent C2 delete_agent_project (best-effort).
# --------------------------------------------------------------------------- #


def test_delete_cascade_removes_langfuse_project(delete_fakes):
    f = delete_fakes
    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    # The C2 teardown fired exactly once, with the captured agent (which carries the
    # langfuse_project_id / langfuse_key_secret_name envelope it reads).
    f.langfuse.delete_agent_project.assert_called_once_with(f.agent)
    # E26/T7: it is now a REPORTED cascade line-item — success surfaces as "deleted" so
    # the operator SEES the Langfuse project/secret was reclaimed.
    langfuse = next(i for i in res.items if i.item == "langfuse")
    assert langfuse.outcome == "deleted"


def test_delete_cascade_langfuse_absent_is_success(delete_fakes):
    f = delete_fakes
    # Simulate a Langfuse teardown blow-up (already-gone / unreachable). The cascade step
    # is REPORTED (so a failure is visible as its own item) but NON-BLOCKING — a raise
    # here can never abort the cascade nor trap the row; the overall delete still completes.
    f.langfuse.delete_agent_project.side_effect = RuntimeError("langfuse unreachable")
    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    # The langfuse item is PRESENT + failed (visible to the operator), yet the cascade
    # continues: identity + record still succeed and the row is gone.
    langfuse = next(i for i in res.items if i.item == "langfuse")
    assert langfuse.outcome == "failed"
    # SAFE reason — the exception TYPE only, never the message/secret.
    assert langfuse.reason == "RuntimeError"
    assert "unreachable" not in (langfuse.reason or "")
    assert next(i for i in res.items if i.item == "identity").outcome == "deleted"
    assert next(i for i in res.items if i.item == "record").outcome == "deleted"
    assert res.record_removed is True
    assert f.svc._get_repo(R) is None


def test_delete_cascade_langfuse_reported_failed_does_not_block_record(delete_fakes):
    """A Langfuse teardown failure is REPORTED (its own failed item) yet NON-BLOCKING:
    it must not feed the record-gating predicate, so the record is still deleted and the
    DDB row is gone. This is the reported-but-non-blocking guarantee."""
    f = delete_fakes
    f.langfuse.delete_agent_project.side_effect = RuntimeError("boom")
    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    # The failed langfuse item is in the result...
    assert next(i for i in res.items if i.item == "langfuse").outcome == "failed"
    # ...but every OTHER selected step succeeded, so the record is deleted and the row gone.
    assert next(i for i in res.items if i.item == "record").outcome == "deleted"
    assert res.record_removed is True
    assert f.registry.deleted == AGENT_ID
    assert f.svc._get_repo(R) is None


def test_delete_cascade_identity_deselected_marks_langfuse_skipped(delete_fakes):
    """Langfuse is grouped with identity (the agent's own resource). Deselecting identity
    skips the Langfuse teardown too — but it is emitted as an explicit "skipped" item (never
    silently absent) so the orphaned Langfuse project stays VISIBLE in the result, while the
    record still deletes."""
    f = delete_fakes
    sel = RepoDeleteSelection(identity=False)
    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=sel)

    assert next(i for i in res.items if i.item == "identity").outcome == "skipped"
    langfuse = next(i for i in res.items if i.item == "langfuse")
    assert langfuse.outcome == "skipped"
    # Deselected ⇒ the C2 teardown is never invoked.
    f.langfuse.delete_agent_project.assert_not_called()
    # A skipped (not failed) langfuse item does not block the record.
    assert next(i for i in res.items if i.item == "record").outcome == "deleted"
    assert res.record_removed is True
    assert f.svc._get_repo(R) is None


def test_delete_preview_unaffected(delete_fakes):
    f = delete_fakes
    # The T7 langfuse teardown is a delete-time step only — the READ-ONLY preview probe
    # offers the same artifacts and never touches Langfuse.
    preview = f.svc.preview_delete(project_id=P, repo_id=R)

    assert [i.item for i in preview.items] == [
        "github",
        "image",
        "runtime",
        "identity",
        "record",
    ]
    f.langfuse.delete_agent_project.assert_not_called()


# --------------------------------------------------------------------------- #
# preview_delete — the E23/T11 reachability pre-check (READ-ONLY, deletes NOTHING)
# --------------------------------------------------------------------------- #


def _preview_states(preview: RepoDeletePreview) -> dict:
    return {i.item: i.state for i in preview.items}


def test_preview_delete_all_present(delete_fakes):
    f = delete_fakes
    preview = f.svc.preview_delete(project_id=P, repo_id=R)

    states = _preview_states(preview)
    assert states["github"] == "present"
    assert states["image"] == "present"
    assert states["runtime"] == "present"
    assert states["identity"] == "present"
    assert states["record"] == "present"
    # A read-only probe: NOTHING is deleted.
    assert f.registry.deleted is None
    assert f.identity.deleted_runtime_arn is None
    assert f.ecr.deleted_agent_id is None
    assert f.github.deleted is None


def test_preview_delete_github_gone_when_connection_missing(delete_fakes):
    f = delete_fakes
    f.svc._conn.get_connection.side_effect = ConnectionError(
        "Unknown connection", kind="not_found"
    )
    states = _preview_states(f.svc.preview_delete(project_id=P, repo_id=R))
    assert states["github"] == "gone"


def test_preview_delete_github_gone_when_repo_absent(delete_fakes):
    f = delete_fakes
    f.github.repo_exists_return = False
    states = _preview_states(f.svc.preview_delete(project_id=P, repo_id=R))
    assert states["github"] == "gone"


def test_preview_delete_github_unknown_on_probe_error(delete_fakes):
    f = delete_fakes
    f.github.repo_exists_raises = GitHubRepoError("timeout")
    states = _preview_states(f.svc.preview_delete(project_id=P, repo_id=R))
    assert states["github"] == "unknown"


def test_preview_delete_image_gone_when_none(delete_fakes):
    f = delete_fakes
    f.ecr.count_images_return = 0
    states = _preview_states(f.svc.preview_delete(project_id=P, repo_id=R))
    assert states["image"] == "gone"


def test_preview_delete_image_unknown_on_probe_error(delete_fakes):
    f = delete_fakes
    f.ecr.count_images_raises = RuntimeError("boom")
    states = _preview_states(f.svc.preview_delete(project_id=P, repo_id=R))
    assert states["image"] == "unknown"


def test_preview_delete_runtime_gone_when_probe_says_gone(delete_fakes):
    f = delete_fakes
    f.identity.runtime_exists_return = False
    states = _preview_states(f.svc.preview_delete(project_id=P, repo_id=R))
    assert states["runtime"] == "gone"


def test_preview_delete_runtime_unknown_on_probe_error(delete_fakes):
    f = delete_fakes
    f.identity.runtime_exists_raises = RuntimeError("boom")
    states = _preview_states(f.svc.preview_delete(project_id=P, repo_id=R))
    assert states["runtime"] == "unknown"


def test_preview_delete_runtime_unknown_on_access_denied(delete_fakes):
    # AccessDenied is AMBIGUOUS: runtime_exists RAISES (never infers gone), so a
    # possibly-live runtime maps to "unknown" (frontend offers it, checked) — NEVER
    # "gone". This is the anti-silent-orphan guarantee at the preview layer.
    f = delete_fakes
    f.identity.runtime_exists_raises = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "no"}},
        "GetAgentRuntime",
    )
    states = _preview_states(f.svc.preview_delete(project_id=P, repo_id=R))
    assert states["runtime"] == "unknown"


def test_preview_delete_runtime_gone_when_agent_missing(delete_fakes):
    f = delete_fakes
    f.registry.get_return = None  # no agent → nothing provisioned
    states = _preview_states(f.svc.preview_delete(project_id=P, repo_id=R))
    assert states["runtime"] == "gone"
    assert states["identity"] == "gone"


def test_preview_delete_identity_present_from_stored_ids(delete_fakes):
    # Stored-id heuristic — NO live Graph probe: present if entra ids are on the record.
    f = delete_fakes
    states = _preview_states(f.svc.preview_delete(project_id=P, repo_id=R))
    assert states["identity"] == "present"


def test_preview_delete_identity_gone_when_no_ids(delete_fakes):
    f = delete_fakes
    f.registry.get_return = SimpleNamespace(
        id=AGENT_ID, agent_arn=None, entra_app_id=None, entra_sp_id=None
    )
    states = _preview_states(f.svc.preview_delete(project_id=P, repo_id=R))
    assert states["identity"] == "gone"


def test_preview_delete_unknown_repo_raises_not_found(delete_fakes):
    f = delete_fakes
    with pytest.raises(ProjectError) as e:
        f.svc.preview_delete(project_id=P, repo_id="nope")
    assert e.value.kind == "not_found"


def test_preview_delete_wrong_project_raises_not_found(delete_fakes):
    f = delete_fakes
    with pytest.raises(ProjectError) as e:
        f.svc.preview_delete(project_id="other", repo_id=R)
    assert e.value.kind == "not_found"


# --------------------------------------------------------------------------- #
# delete_project — guarded delete (blocked while repos exist, E23/T5)
# --------------------------------------------------------------------------- #

P_EMPTY = "proj-empty"
P_WITH_REPO = "proj-with-repo"


def _make_project_with(svc, project_id):
    """Persist an empty project container into the local store under ``project_id``."""
    svc._save_project(
        Project(
            id=project_id,
            name="p",
            connection_id="c1",
            tenant_id="default",
            description="d",
            created_by="op@x",
            created_at="2026-07-02T00:00:00+00:00",
            updated_at="2026-07-02T00:00:00+00:00",
        )
    )


@pytest.fixture
def delete_project_svc(project_service_fakes):
    """A service seeded with an empty project and one that holds a repository."""
    svc = project_service_fakes
    _make_project_with(svc, P_EMPTY)
    _make_project_with(svc, P_WITH_REPO)
    svc._save_repo(
        Repository(
            id="repo-in-p",
            project_id=P_WITH_REPO,
            name="my-agent",
            repo_url="https://github.com/acme/my-agent",
            agent_id="agent-1",
            template_name=TEMPLATE_NAME,
            cicd_status="provisioning",
            status="provisioning",
            created_by="op@x",
            created_at="2026-07-02T00:00:00+00:00",
            updated_at="2026-07-02T00:00:00+00:00",
        )
    )
    return svc


def test_delete_project_removes_empty_container(delete_project_svc):
    svc = delete_project_svc
    svc.delete_project(P_EMPTY)
    assert svc._get_project(P_EMPTY) is None


def test_delete_project_blocked_when_repos_exist(delete_project_svc):
    svc = delete_project_svc
    with pytest.raises(ProjectError) as e:
        svc.delete_project(P_WITH_REPO)
    assert e.value.kind == "has_repositories"
    assert svc._get_project(P_WITH_REPO) is not None  # not deleted


def test_delete_project_unknown_raises_not_found(delete_project_svc):
    svc = delete_project_svc
    with pytest.raises(ProjectError) as e:
        svc.delete_project("nope")
    assert e.value.kind == "not_found"


# --------------------------------------------------------------------------- #
# Stage-scoped runtime Terraform state key (E28/T2, design D5)
#
# Before T2 the runtime state key was `agentcore-runtime/{agent_id}/terraform.tfstate`
# with NO stage segment, so a prod deploy re-`terraform apply`ed the SAME state file and
# the SAME `agent_arn` the dev deploy wrote. dev and prod were not environments; they
# were a promotion ceremony in front of ONE mutable runtime — promoting to prod MUTATED
# dev, and there was no state a rollback could roll back TO.
#
# The key is produced in TWO places that must agree byte-for-byte or the pipeline writes
# one object and reads another (a silent split-brain, not an error):
#   * Python — `runtime_state_key`, used by the delete/inspect path
#   * the buildspec — `terraform init -backend-config="key=…"`, the actual writer
# The buildspec is the WRITER, so a disagreement means the delete path silently misses
# every object and TF state leaks forever. Hence the cross-file agreement test below:
# it reads the real buildspec.yml, because a hand-copied literal is exactly the drift
# that would go unnoticed.
# --------------------------------------------------------------------------- #

RUNTIME_STATE_BUCKET = "agp-tf-state"


def test_runtime_state_key_carries_the_stage_segment():
    """D5: the key is `agentcore-runtime/{agent_id}/{stage}/terraform.tfstate`. Without the
    stage segment two stages share one state file and one runtime ARN."""
    from services.project_service import runtime_state_key

    assert (
        runtime_state_key("a-1", "dev")
        == "agentcore-runtime/a-1/dev/terraform.tfstate"
    )


@pytest.mark.parametrize("stage", ["dev", "prod", "uat", "staging", "eu-west-1"])
def test_runtime_state_key_is_distinct_per_stage_for_ANY_stage_name(stage):
    """D8: the stage set is OPEN — a tenant may carry `uat` only, or per-region stages. The
    key must scope by whatever the stage is CALLED, never by a dev/prod literal."""
    from services.project_service import runtime_state_key

    key = runtime_state_key("a-1", stage)
    assert key == f"agentcore-runtime/a-1/{stage}/terraform.tfstate"
    assert f"/{stage}/" in key
    # …and no two stages can ever collide on one state file.
    assert key != runtime_state_key("a-1", "other")


def test_runtime_state_key_agrees_with_the_buildspec_that_actually_writes_it():
    """The two producers must agree. The buildspec is the WRITER (`terraform init
    -backend-config="key=…"`); Python only deletes/inspects. A drifted literal means the
    delete path targets an object that does not exist and state leaks silently — so this
    asserts against the REAL buildspec.yml rather than a copied string."""
    import re
    from pathlib import Path

    from services.project_service import runtime_state_key

    buildspec = (
        Path(__file__).resolve().parents[2]
        / "infrastructure"
        / "modules"
        / "codebuild"
        / "buildspec.yml"
    )
    assert buildspec.is_file(), buildspec

    keys = re.findall(
        r'-backend-config="key=(agentcore-runtime/[^"]+)"', buildspec.read_text()
    )
    assert len(keys) == 1, f"expected exactly one runtime state key, got {keys}"
    # The shell literal, with the buildspec's own variables substituted.
    resolved = keys[0].replace("$AGENT_ID", "a-1").replace("$STAGE", "dev")
    assert resolved == runtime_state_key("a-1", "dev")


def test_delete_repo_deletes_the_tf_state_of_EVERY_stage(delete_fakes):
    """The consequence of stage-scoping the key: there is no longer ONE state object per
    agent, there are N (one per stage ever deployed). A single-key delete would leave every
    other stage's state behind, so the teardown deletes the whole
    `agentcore-runtime/{agent_id}/` prefix — which also covers a stage the tenant has since
    dropped, whose state a `tenant.stages` loop would never see."""
    f = delete_fakes
    f.s3.keys = [
        f"agentcore-runtime/{AGENT_ID}/dev/terraform.tfstate",
        f"agentcore-runtime/{AGENT_ID}/prod/terraform.tfstate",
        f"agentcore-runtime/{AGENT_ID}/uat/terraform.tfstate",
    ]

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    assert f.s3.listed_prefix == f"agentcore-runtime/{AGENT_ID}/"
    assert sorted(f.s3.deleted_keys) == sorted(f.s3.keys)
    assert next(i for i in res.items if i.item == "runtime").outcome == "deleted"


def test_delete_repo_tolerates_an_agent_with_no_stored_state(delete_fakes):
    """Nothing ever deployed (or the state was already reclaimed) ⇒ an empty listing. That is
    an already-done success, not a failure — and no delete call is made."""
    f = delete_fakes
    f.s3.keys = []

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    assert f.s3.deleted_keys == []
    assert next(i for i in res.items if i.item == "runtime").outcome == "deleted"


# --------------------------------------------------------------------------- #
# Fix round 1 — the teardown listing/delete loop is bounded and per-key tolerant.
# Both close asymmetries with the deployment read: that path got a page ceiling for the
# "store keeps handing back a token" case, and this one had an unbounded `while True`.
# --------------------------------------------------------------------------- #


def test_state_listing_stops_at_the_page_ceiling_instead_of_paging_forever(delete_fakes):
    """A store that always returns a continuation token would hang the whole teardown. The
    listing stops at ``_MAX_STATE_LIST_PAGES``; truncating is allowed by this path's
    best-effort contract, but it IS a leak, so it must be bounded rather than infinite."""
    from services.project_service import _MAX_STATE_LIST_PAGES

    f = delete_fakes
    f.s3.endless_pages = True

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    assert f.s3.list_calls == _MAX_STATE_LIST_PAGES
    # Everything it DID see is still deleted — a bounded reclaim, not an abandoned one.
    assert len(f.s3.deleted_keys) == _MAX_STATE_LIST_PAGES
    assert next(i for i in res.items if i.item == "runtime").outcome == "deleted"


def test_one_failing_state_delete_does_not_strand_the_others(delete_fakes):
    """Per-key tolerance. The objects are independent, so a ``ClientError`` on the SECOND of
    three must not abandon the third — a partial reclaim beats leaking two objects because one
    was denied. Still never raises: the runtime item stays ``deleted``."""
    f = delete_fakes
    dev, prod, uat = (
        f"agentcore-runtime/{AGENT_ID}/dev/terraform.tfstate",
        f"agentcore-runtime/{AGENT_ID}/prod/terraform.tfstate",
        f"agentcore-runtime/{AGENT_ID}/uat/terraform.tfstate",
    )
    f.s3.keys = [dev, prod, uat]
    f.s3.delete_raises_for = {
        prod: ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, "DeleteObject")
    }

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    assert f.s3.deleted_keys == [dev, uat]  # the third was still attempted, and succeeded
    assert next(i for i in res.items if i.item == "runtime").outcome == "deleted"


def test_a_failed_state_listing_is_swallowed_and_deletes_nothing(delete_fakes):
    """If the LISTING itself fails there is no inventory to act on — log and leave the runtime
    item successful (the runtime is gone; its state is a best-effort extra)."""
    f = delete_fakes
    f.s3.raises = ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, "ListObjectsV2")

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    assert f.s3.deleted_keys == []
    assert next(i for i in res.items if i.item == "runtime").outcome == "deleted"


# --------------------------------------------------------------------------- #
# E28A/T2 — the IAM exec role is RECLAIMED by the delete cascade
#
# Finding #15, verified live: the cascade deleted the AgentCore runtime AND the Terraform
# state object that was the role's only record, so the role became unreachable by IaC and
# untracked by AGP in the same operation. Five orphaned `*-agentcore-exec` roles existed in
# the account, all from deleted repos.
#
# This is not merely litter. IAM role names are account-global and were never reclaimed, so
# a repo re-materialized under the SAME agent name hits `EntityAlreadyExists` on the
# module's `aws_iam_role.exec`. Stage-scoping the name (a later change) multiplies the leak
# by the stage count, which is why the reclaim lands FIRST.
# --------------------------------------------------------------------------- #

# E28A/T1b stage-scoped the role name, so the PRE-T1b shape is now the LEGACY name. It is still
# the name the DEFAULT fixture agent (a scalar-only legacy record, `agent_arns={}`) resolves to —
# it knows no stage — which is why the pre-existing cascade assertions below still read this
# constant. A post-T1b record's per-stage names are built with `_exec_role(stage)`.
LEGACY_EXEC_ROLE = f"{AGENT_NAME}-agentcore-exec"


def _exec_role(stage):
    """The per-stage exec-role name, spelled out here rather than imported from the producer.

    Deliberately a restated literal: importing `agentcore_exec_role_name` would make every
    assertion below a tautology (`f(x) == f(x)`) that a broken producer satisfies. The literal is
    what pins the producer, and the producer is separately pinned against the real main.tf by
    `test_exec_role_name_matches_the_terraform_module_that_creates_it`."""
    return f"{AGENT_NAME}-{stage}-agentcore-exec"


def _iam_client_error(code, op):
    return ClientError({"Error": {"Code": code, "Message": "x"}}, op)


def _deleted_roles(f):
    """The set of RoleNames `delete_role` was called with — order-free by construction.

    A `set`, deliberately: the reclaim iterates `agent_arns`, so an assertion on a LIST would be
    an assertion about dict iteration order, which is the defect this epic already had to repair
    once in `test_preview_reports_the_runtime_PRESENT_…`. Ordering that IS load-bearing (the
    policy before its own role) is asserted separately, per role, off `mock_calls`."""
    return {c.kwargs["RoleName"] for c in f.iam.delete_role.call_args_list}


def _deleted_role_policies(f):
    """The set of RoleNames `delete_role_policy` was called with. See :func:`_deleted_roles`."""
    return {c.kwargs["RoleName"] for c in f.iam.delete_role_policy.call_args_list}


def _agentcore_main_tf():
    from pathlib import Path

    main_tf = (
        Path(__file__).resolve().parents[2]
        / "infrastructure"
        / "modules"
        / "agentcore_runtime"
        / "main.tf"
    )
    assert main_tf.is_file(), main_tf
    return main_tf.read_text()


def _tf_exec_role_name(body, agent_name, stage):
    """The exec-role name the REAL module would create for ``agent_name`` at ``stage``.

    Resolved in two structural hops rather than by grepping for a name-shaped string, because
    T1b moved the expression out of the resource and into a `locals` block: the resource's `name`
    is now an unquoted `local.<ref>`, and `<ref>`'s template lives elsewhere in the file. Grepping
    for a quoted literal on the resource is exactly what stopped working, so this follows the
    reference instead — and it will keep working (or fail LOUDLY) if the expression moves again.

    ANTI-PROSE, and by ANCHORING rather than by stripping. main.tf's comments discuss this exact
    name at length (they quote the template AND name the Python producer), which is the trap that
    defeated five guards across E28/E28A. Both patterns below anchor the assignment at LINE START
    (`^\\s*`, `re.M`), and a whole-line HCL comment cannot form one — the `#` occupies the first
    non-space column, so the prose is unreachable rather than merely filtered out. A pre-pass that
    stripped comments was tried and REMOVED: deleting it changed no result, so it was a defense
    that read as load-bearing while doing nothing. `…cannot_be_satisfied_by_a_COMMENT` proves the
    anchor is what does the work."""
    import re

    code = body
    refs = re.findall(
        r'^\s*resource\s+"aws_iam_role"\s+"exec"\s*\{\s*\n\s*name\s*=\s*local\.(\w+)\s*$',
        code,
        re.M,
    )
    assert len(refs) == 1, f"expected exactly one exec role name reference, got {refs}"
    templates = re.findall(rf'^\s*{re.escape(refs[0])}\s*=\s*"([^"]+)"', code, re.M)
    assert len(templates) == 1, f"expected exactly one {refs[0]} definition, got {templates}"
    rendered = templates[0].replace("${var.agent_name}", agent_name).replace("${var.stage}", stage)
    # Nothing may be left unresolved. Without this a THIRD interpolation added to the template
    # would silently survive into the "expected" value and be compared against itself.
    assert "${" not in rendered, f"unresolved interpolation in {rendered!r}"
    return rendered


def test_exec_role_name_matches_the_terraform_module_that_creates_it(delete_fakes):
    """The name is pinned against the REAL module, not a copied string.

    Terraform CREATES the role; this code only deletes it, so a drift is silent — the
    teardown would delete a name that never existed and report success while the real role
    leaked. Same cross-file-agreement reasoning as the runtime-state-key/buildspec test.

    E28A/T1b stage-scoped the name and this test duly went RED, which was its purpose. It is now
    updated to pin the STAGE dimension too, so it still fails if either producer drops or renames
    a segment — the module's template is rendered for a stage and compared byte-for-byte against
    what the backend derives for the same inputs."""
    from services.project_service import agentcore_exec_role_name

    body = _agentcore_main_tf()

    for stage in ("dev", "prod"):
        assert _tf_exec_role_name(body, AGENT_NAME, stage) == agentcore_exec_role_name(
            AGENT_NAME, stage
        )
    # The STAGE is genuinely part of the name — not merely accepted and dropped. Without this a
    # producer that ignored its `stage` argument would still agree with a module template that
    # had also dropped `${var.stage}`; this pins the two stages apart from each other.
    assert agentcore_exec_role_name(AGENT_NAME, "dev") != agentcore_exec_role_name(
        AGENT_NAME, "prod"
    )
    assert agentcore_exec_role_name(AGENT_NAME, "dev") == _exec_role("dev")
    # …and the derivation is from the agent NAME, never the id.
    assert AGENT_ID not in agentcore_exec_role_name(AGENT_NAME, "dev")
    # The LEGACY producer keeps producing the pre-T1b shape, which is NOT what the module makes
    # any more — that difference is the whole reason both names are attempted on delete.
    from services.project_service import legacy_agentcore_exec_role_name

    assert legacy_agentcore_exec_role_name(AGENT_NAME) == LEGACY_EXEC_ROLE
    assert legacy_agentcore_exec_role_name(AGENT_NAME) != agentcore_exec_role_name(
        AGENT_NAME, "dev"
    )


def test_the_terraform_name_extraction_cannot_be_satisfied_by_a_COMMENT(delete_fakes):
    """Non-vacuity for the guard above: it must read CODE, never the module's prose.

    main.tf's comments state the role name's contract in words AND quote the template verbatim, so
    a source-as-text assertion is one careless regex away from passing against a comment while the
    resource says something else. Fed synthetic modules whose ONLY mention of the name is a
    comment, the extraction must refuse (assert) rather than return a name — which is what makes
    the real pin load-bearing instead of decorative. This is also the test that justifies dropping
    the comment-stripping pre-pass: the ANCHOR is the defense, and here it is exercised directly."""
    import pytest

    commented_out = '''
# resource "aws_iam_role" "exec" {
#   name = local.exec_role_name
# }
locals {
  exec_role_name = "${var.agent_name}-${var.stage}-agentcore-exec"
}
'''
    template_only_in_prose = '''
resource "aws_iam_role" "exec" {
  name = local.exec_role_name
}
locals {
  # exec_role_name = "${var.agent_name}-${var.stage}-agentcore-exec"
}
'''
    for source in (commented_out, template_only_in_prose):
        with pytest.raises(AssertionError):
            _tf_exec_role_name(source, AGENT_NAME, "dev")

    # …and the REAL module is not accidentally in that category — it resolves.
    assert _tf_exec_role_name(_agentcore_main_tf(), AGENT_NAME, "dev") == _exec_role("dev")


def test_exec_role_inline_policy_name_matches_the_terraform_module(delete_fakes):
    """The inline policy name is pinned to the module too. IAM refuses to delete a role that
    still carries an inline policy, so a drifted policy name doesn't just leak the policy —
    the ROLE delete then fails with DeleteConflict and nothing is reclaimed."""
    import re
    from pathlib import Path

    from services.project_service import _AGENTCORE_EXEC_POLICY_NAME

    main_tf = (
        Path(__file__).resolve().parents[2]
        / "infrastructure"
        / "modules"
        / "agentcore_runtime"
        / "main.tf"
    )
    body = main_tf.read_text()
    names = re.findall(
        r'resource\s+"aws_iam_role_policy"\s+"exec"\s*\{\s*\n\s*name\s*=\s*"([^"]+)"', body
    )
    assert len(names) == 1, f"expected exactly one exec inline policy name, got {names}"
    assert names[0] == _AGENTCORE_EXEC_POLICY_NAME


def test_delete_cascade_reclaims_the_exec_role_policy_first_then_the_role(delete_fakes):
    """ORDER is load-bearing: IAM rejects `DeleteRole` on a role that still has an inline
    policy (`DeleteConflict`). Deleting the policy first is what makes the role deletable."""
    f = delete_fakes
    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    assert f.iam.mock_calls == [
        call.delete_role_policy(RoleName=LEGACY_EXEC_ROLE, PolicyName="runtime-exec"),
        call.delete_role(RoleName=LEGACY_EXEC_ROLE),
    ]
    # The reclaim rides the runtime step, which stays successful.
    assert next(i for i in res.items if i.item == "runtime").outcome == "deleted"
    assert res.record_removed is True


def test_delete_cascade_treats_an_ALREADY_GONE_exec_role_as_success(delete_fakes):
    """Idempotence. A repo predating the module, or a second delete attempt, has no role —
    `NoSuchEntity` on EITHER call is the already-done state, not a failure. It must not flip
    the runtime item, and the policy's NoSuchEntity must not stop the role delete from being
    attempted (they are reported independently by IAM)."""
    f = delete_fakes
    f.iam.delete_role_policy.side_effect = _iam_client_error("NoSuchEntity", "DeleteRolePolicy")
    f.iam.delete_role.side_effect = _iam_client_error("NoSuchEntity", "DeleteRole")

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    # The role delete was still ATTEMPTED despite the policy being absent.
    f.iam.delete_role.assert_called_once_with(RoleName=LEGACY_EXEC_ROLE)
    assert next(i for i in res.items if i.item == "runtime").outcome == "deleted"
    assert res.record_removed is True


def test_an_iam_failure_does_NOT_abort_the_cascade(delete_fakes):
    """Same contract as `_delete_runtime_state`: the runtime being gone matters more than the
    role. An `AccessDenied` (the live shape if the task role lacks the grant) does NOT flip the
    runtime item, the LATER steps still run, and the DDB row is still reclaimed rather than
    trapped behind a leaked role.

    E28C/T5 CORRECTED WHAT THIS TEST ASSERTED, not what it is for. The final line used to read
    `["deleted"] * 6` — under an INJECTED AccessDenied. That is the test pinning the lie: every
    item reported `deleted` while an account-global IAM role survived, which is precisely how six
    of them accumulated unnoticed. Not aborting the cascade and reporting a clean teardown are
    two different things, and only the first is the contract here. The denial now surfaces on its
    OWN non-blocking `exec_role` item; everything this test was protecting still holds."""
    f = delete_fakes
    f.iam.delete_role.side_effect = _iam_client_error("AccessDenied", "DeleteRole")

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    assert next(i for i in res.items if i.item == "runtime").outcome == "deleted"
    # Later steps ran and the row is gone — nothing downstream was skipped.
    assert f.identity.deleted_identity is True
    f.langfuse.delete_agent_project.assert_called_once_with(f.agent)
    assert res.record_removed is True
    assert f.svc._get_repo(R) is None
    # Honest, and still non-blocking: exactly ONE item reports the failure.
    assert {i.item: i.outcome for i in res.items} == {
        "github": "deleted",
        "image": "deleted",
        "runtime": "deleted",
        "exec_role": "failed",
        "identity": "deleted",
        "langfuse": "deleted",
        "record": "deleted",
    }


def test_an_iam_policy_delete_failure_still_attempts_the_role(delete_fakes):
    """A non-NoSuchEntity failure on the POLICY must not skip the role delete. The two are
    independent reclaims and a partial one beats leaking both — same per-key tolerance the
    state-object teardown has."""
    f = delete_fakes
    f.iam.delete_role_policy.side_effect = _iam_client_error("AccessDenied", "DeleteRolePolicy")

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    f.iam.delete_role.assert_called_once_with(RoleName=LEGACY_EXEC_ROLE)
    assert next(i for i in res.items if i.item == "runtime").outcome == "deleted"


def test_deselecting_the_runtime_leaves_the_exec_role_ALONE(delete_fakes):
    """The reclaim rides the runtime selection. An operator who unchecks `runtime` is keeping
    the runtime, which still NEEDS its exec role to pull images and write logs — deleting the
    role would silently break a runtime the operator deliberately kept."""
    f = delete_fakes
    sel = RepoDeleteSelection(runtime=False)

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=sel)

    assert f.iam.mock_calls == []
    assert next(i for i in res.items if i.item == "runtime").outcome == "skipped"


def test_the_exec_role_is_reclaimed_even_when_the_runtime_arn_is_already_gone(delete_fakes):
    """The role outlives the runtime: `_delete_runtime` no-ops when there is no ARN, but the
    ROLE is what leaks account-globally and blocks a re-materialize under the same name. The
    reclaim only needs the agent's name, so a cleared/never-written ARN must not strand it."""
    f = delete_fakes
    f.agent.agent_arn = None

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    f.iam.delete_role.assert_called_once_with(RoleName=LEGACY_EXEC_ROLE)
    assert f.identity.deleted_runtime_arn is None  # the runtime delete correctly no-oped
    assert next(i for i in res.items if i.item == "runtime").outcome == "deleted"


def test_an_agent_with_no_name_reclaims_nothing_rather_than_guessing(delete_fakes):
    """No name ⇒ no derivable role name. Calling IAM with an empty/None `RoleName` would at
    best be a wasted denied call and at worst target something else — so it is skipped."""
    f = delete_fakes
    f.agent.name = None

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    assert f.iam.mock_calls == []
    assert next(i for i in res.items if i.item == "runtime").outcome == "deleted"


# =========================================================================== #
# E28A/T1b FIX — the cascade must reclaim EVERY STAGE's exec role              #
# =========================================================================== #
# T1b stage-scoped the module's role name to `{agent_name}-{stage}-agentcore-exec`, so an agent
# genuinely owns one role PER STAGE. The cascade still derived the old un-scoped name: it deleted
# a name that never existed, IAM answered `NoSuchEntity`, the code (correctly, for idempotency)
# read that as already-done — and BOTH stage roles leaked while the teardown reported clean. IAM
# role names are account-global and nothing else reclaims them, so the next materialize under the
# same agent name hits `EntityAlreadyExists` again. That is finding #9 reproduced by its own fix.


def test_delete_cascade_reclaims_EVERY_STAGE_exec_role(delete_fakes):
    """N stages ⇒ N per-stage roles reclaimed (plus the legacy name, see the sibling below).

    The stages come from `agent_arns`' KEYS — the only evidence on the record of what was ever
    deployed. Deleting one name for an agent with three stage roles leaks two account-global
    names, silently, because every miss reads as `NoSuchEntity`/success."""
    f = delete_fakes
    f.agent.agent_arns = {"dev": DEV_ARN, "prod": PROD_ARN, "uat": UAT_ARN}
    f.agent.agent_arn = UAT_ARN  # C-A2: the scalar mirrors whichever stage deployed last

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    assert _deleted_roles(f) == {
        _exec_role("dev"),
        _exec_role("prod"),
        _exec_role("uat"),
        LEGACY_EXEC_ROLE,
    }
    # Each role's own inline policy was reclaimed too — IAM refuses `DeleteRole` while one
    # remains, so a per-stage role whose policy was skipped is a role that does not get deleted.
    assert _deleted_role_policies(f) == _deleted_roles(f)
    assert next(i for i in res.items if i.item == "runtime").outcome == "deleted"
    assert res.record_removed is True


def test_delete_cascade_ALSO_reclaims_the_pre_T1b_UN_STAGE_SCOPED_name(delete_fakes):
    """THE MIGRATION CASE. Every repo deployed before T1b's rollout has a role at the OLD
    un-stage-scoped name, and five such orphans exist in the account right now.

    The legacy name is attempted even when the map IS populated — same reasoning as
    `_delete_runtime` unioning the scalar in: an agent deployed pre-T1b and redeployed ONCE under
    T1b has a map naming only the redeployed stage while a real role still sits at the old name. A
    populated map is the inventory of what T1b knows about, not proof that nothing predates it.
    Trying it costs one tolerated `NoSuchEntity` pair; not trying leaks the name forever — and it
    is the very name a re-materialize of that agent would need."""
    f = delete_fakes
    f.agent.agent_arns = {"prod": PROD_ARN}

    f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    assert LEGACY_EXEC_ROLE in _deleted_roles(f)
    assert _deleted_roles(f) == {_exec_role("prod"), LEGACY_EXEC_ROLE}


def test_a_legacy_scalar_only_record_does_NOT_derive_an_unknown_stage_role(delete_fakes):
    """`resolve_runtime_arns` keys a scalar-only record under the `UNKNOWN_STAGE` PLACEHOLDER,
    which is not a stage anyone deployed to. Interpolating it would target
    `{name}-unknown-agentcore-exec` — a name Terraform never created — producing a guaranteed
    `NoSuchEntity` that the cascade reads as success while the role that DOES exist (the legacy
    un-scoped one) went untouched. This is the whole registry's shape today, so it is the case a
    naive "iterate the keys" would break on."""
    from models.agent import UNKNOWN_STAGE

    f = delete_fakes
    f.agent.agent_arns = {}  # legacy: scalar only ⇒ resolves to {UNKNOWN_STAGE: arn}

    f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    assert _deleted_roles(f) == {LEGACY_EXEC_ROLE}
    assert all(UNKNOWN_STAGE not in name for name in _deleted_roles(f))


def test_the_reclaim_deletes_each_roles_POLICY_before_that_ROLE(delete_fakes):
    """Order is load-bearing PER ROLE, and asserting it globally would be order-dependent noise.

    IAM rejects `DeleteRole` while the role still carries an inline policy (`DeleteConflict`), so
    what must hold is: for every role, its own policy delete precedes its own role delete. The
    order the ROLES are visited in is dict-iteration order and deliberately NOT asserted."""
    f = delete_fakes
    f.agent.agent_arns = {"dev": DEV_ARN, "prod": PROD_ARN}

    f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    seen = [(c[0], c.kwargs["RoleName"]) for c in f.iam.mock_calls]
    for role in (_exec_role("dev"), _exec_role("prod"), LEGACY_EXEC_ROLE):
        assert seen.index(("delete_role_policy", role)) < seen.index(("delete_role", role)), role
    # Non-vacuity: every call above was actually observed, so the index lookups cannot pass by
    # comparing two positions that happen to exist for unrelated reasons.
    assert len(seen) == 6


def test_one_stage_exec_role_failing_does_NOT_strand_the_others(delete_fakes):
    """Per-ROLE tolerance, mirroring `_delete_runtime_state`'s per-KEY tolerance. An
    `AccessDenied` on one role (the live shape while the task-role grant is missing) must not
    abandon the rest — a partial reclaim strictly beats leaking every remaining name, and the
    cascade still must not fail."""
    f = delete_fakes
    f.agent.agent_arns = {"dev": DEV_ARN, "prod": PROD_ARN}
    boom = _iam_client_error("AccessDenied", "DeleteRole")
    f.iam.delete_role.side_effect = [boom, None, None]

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    # All three were ATTEMPTED even though the first blew up.
    assert _deleted_roles(f) == {_exec_role("dev"), _exec_role("prod"), LEGACY_EXEC_ROLE}
    assert next(i for i in res.items if i.item == "runtime").outcome == "deleted"
    assert res.record_removed is True


def test_the_reclaim_name_list_is_deduped_and_holds_no_unresolved_placeholder(delete_fakes):
    """The list handed to IAM must contain no duplicate and no fabricated segment.

    Asserted on the derivation directly (rather than through a cascade run) because the pathology
    it guards is a name-SHAPE one: a duplicate wastes a call and makes the count unassertable,
    and `None`/`unknown` reaching a `RoleName` is the "delete something that never existed" bug
    in miniature."""
    from services.project_service import ProjectService

    agent = SimpleNamespace(
        agent_arns={"dev": DEV_ARN, "prod": PROD_ARN}, agent_arn=PROD_ARN, name=AGENT_NAME
    )
    names = ProjectService._exec_role_names_to_delete(agent, AGENT_NAME)

    assert len(names) == len(set(names))
    assert sorted(names) == sorted([_exec_role("dev"), _exec_role("prod"), LEGACY_EXEC_ROLE])
    for name in names:
        assert "None" not in name and "unknown" not in name
        assert name.startswith(f"{AGENT_NAME}-") and name.endswith("-agentcore-exec")
        # IAM's own hard cap, which the module also asserts as a `precondition`.
        assert len(name) <= 64


def test_an_empty_or_None_stage_key_is_SKIPPED_rather_than_interpolated(delete_fakes):
    """A malformed map (an empty-string or None key, e.g. a half-written envelope) must not
    produce `{name}--agentcore-exec` or `{name}-None-agentcore-exec`. Those are legal-LOOKING
    names that exist nowhere, so every one of them is a silent no-op that displaces nothing."""
    from services.project_service import ProjectService

    agent = SimpleNamespace(
        agent_arns={"": DEV_ARN, None: PROD_ARN, "dev": UAT_ARN}, agent_arn=None, name=AGENT_NAME
    )
    names = ProjectService._exec_role_names_to_delete(agent, AGENT_NAME)

    assert sorted(names) == sorted([_exec_role("dev"), LEGACY_EXEC_ROLE])


# =========================================================================== #
# E28A/T1 — the delete cascade must reclaim EVERY per-stage runtime           #
# =========================================================================== #
# T1b stage-scopes the runtime module's resource names, so two runtimes genuinely co-exist
# per agent. `_delete_runtime` deleted the ONE stored scalar ARN, which means the other
# runtime survived every repo delete — the leak D-A4 lists first, and the reason T1 is gated
# on T2's IAM reclaim already being in place.

# Per-stage ARNs for a map-bearing (post-T1b) record. Account id is an obviously-fake 12-digit.
DEV_ARN = "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/agent-1_dev-aaa"
PROD_ARN = "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/agent-1_prod-bbb"
UAT_ARN = "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/agent-1_uat-ccc"


def test_delete_cascade_deletes_EVERY_runtime_in_the_per_stage_map(delete_fakes):
    """N runtimes ⇒ N deletes. A record carrying `agent_arns` owns one runtime per stage it
    was ever deployed to, and deleting only the scalar would leave the others running (and
    billing) after the repo, the record and the row are all gone — unreachable by IaC and
    untracked by AGP, exactly the shape of the five orphaned IAM roles D-A2 found live."""
    f = delete_fakes
    f.agent.agent_arns = {"dev": DEV_ARN, "prod": PROD_ARN, "uat": UAT_ARN}
    f.agent.agent_arn = UAT_ARN  # C-A2: the scalar mirrors whichever stage deployed last

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    assert sorted(f.identity.deleted_runtime_arns) == sorted([DEV_ARN, PROD_ARN, UAT_ARN])
    assert next(i for i in res.items if i.item == "runtime").outcome == "deleted"
    assert res.record_removed is True


def test_delete_cascade_deletes_EXACTLY_ONE_runtime_for_a_legacy_record(delete_fakes):
    """The other half of the same contract: a LEGACY record (no `agent_arns` key at all) must
    still delete exactly its ONE scalar runtime — not zero, and not a fabricated per-stage set.
    Every agent in the registry today is this shape, so a regression here breaks every delete."""
    f = delete_fakes
    f.agent.agent_arns = {}

    f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    assert f.identity.deleted_runtime_arns == [AGENT_ARN]


def test_delete_cascade_ignores_the_scalar_when_the_map_names_it_too(delete_fakes):
    """C-A2 has the buildspec write BOTH the map entry and the scalar, so the scalar is a
    DUPLICATE of whichever stage deployed last. Unioning them would call `DeleteAgentRuntime`
    on that runtime twice — harmless but dishonest in the logs, and it would make the N-delete
    count unassertable. A populated map is the complete inventory."""
    f = delete_fakes
    f.agent.agent_arns = {"dev": DEV_ARN, "prod": PROD_ARN}
    f.agent.agent_arn = PROD_ARN  # the live shape: the scalar mirrors the last deploy

    f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    assert sorted(f.identity.deleted_runtime_arns) == sorted([DEV_ARN, PROD_ARN])


def test_delete_cascade_still_deletes_a_scalar_the_map_does_NOT_name(delete_fakes):
    """The migration shape: an agent deployed under pre-T1b code (scalar written, no map), then
    redeployed once under T1b (map gains `prod` only). The scalar still names a REAL, RUNNING
    dev runtime that no map entry covers, and dropping it would leak the very runtime the
    pre-E28A record was tracking. A populated map is the inventory of what T1b knows about —
    not proof that nothing else exists."""
    f = delete_fakes
    f.agent.agent_arns = {"prod": PROD_ARN}
    f.agent.agent_arn = AGENT_ARN  # the pre-T1b runtime, unnamed by the map

    f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    assert sorted(f.identity.deleted_runtime_arns) == sorted([AGENT_ARN, PROD_ARN])


def test_one_runtime_delete_failing_does_NOT_strand_the_others(delete_fakes):
    """Per-ARN tolerance, mirroring `_delete_runtime_state`'s per-KEY tolerance and for the
    same reason: the runtimes are independent, so a partial reclaim strictly beats abandoning
    the rest. The step still reports FAILED (unlike the best-effort state/IAM reclaims — a
    runtime that survives is a live, billing resource, and the row must stay for retry), but
    only AFTER every other runtime was attempted."""
    f = delete_fakes
    f.agent.agent_arns = {"dev": DEV_ARN, "prod": PROD_ARN, "uat": UAT_ARN}
    f.agent.agent_arn = UAT_ARN  # C-A2: the scalar mirrors the last stage deployed
    boom = RuntimeError("AccessDenied")

    real_delete = f.identity.delete_runtime

    def flaky(agent_arn, *, control_client=None):  # E36/T8 kwarg
        if agent_arn == PROD_ARN:
            raise boom
        real_delete(agent_arn, control_client=control_client)

    f.identity.delete_runtime = flaky

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    # dev + uat were still attempted despite prod blowing up.
    assert sorted(f.identity.deleted_runtime_arns) == sorted([DEV_ARN, UAT_ARN])
    # The step is honest about the surviving runtime, and the row is kept for retry.
    assert next(i for i in res.items if i.item == "runtime").outcome == "failed"
    assert res.record_removed is False


def test_a_failed_runtime_delete_logs_the_RID_and_NOT_the_account_bearing_ARN(delete_fakes, caplog):
    """The per-runtime failure log must name the runtime WITHOUT its AWS account id.

    A full runtime ARN is `arn:aws:bedrock-agentcore:<region>:<ACCOUNT>:runtime/<rid>`, and a hard
    project rule bans an account id anywhere — logs included. The RID alone still identifies the
    runtime uniquely, so the line stays chaseable; the sibling reclaims are careful the same way
    (the state delete logs the S3 key, the exec-role reclaim logs the role name).

    Asserted STRUCTURALLY on the emitted log records, not on the source text: the account id is
    read off the ARN constant by position rather than written as a literal, so this guard cannot
    be defeated by prose (there is no forbidden string in this file for a comment to quote), and
    it stays true if the fixture ARNs change."""
    f = delete_fakes
    f.agent.agent_arns = {"dev": DEV_ARN, "prod": PROD_ARN}
    f.agent.agent_arn = PROD_ARN  # C-A2: the scalar mirrors the last stage deployed
    f.identity.delete_runtime_raises = RuntimeError("AccessDenied")

    with caplog.at_level(logging.ERROR, logger="services.project_service"):
        f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    failures = [
        r.getMessage()
        for r in caplog.records
        if r.name == "services.project_service" and "runtime delete failed" in r.getMessage()
    ]
    # Both surviving runtimes are individually nameable — that is why the line exists at all.
    assert len(failures) == 2
    dev_rid, prod_rid = DEV_ARN.rsplit("/", 1)[-1], PROD_ARN.rsplit("/", 1)[-1]
    assert {dev_rid, prod_rid} == {m.rsplit(" ", 1)[-1] for m in failures}
    # ...and no account id, region, or whole ARN rode along. Both derived from the ARN, never
    # spelled out here.
    account_id = DEV_ARN.split(":")[4]
    region = DEV_ARN.split(":")[3]
    for message in failures:
        assert account_id not in message
        assert region not in message
        assert "arn:" not in message


def test_the_exec_role_is_still_reclaimed_when_a_runtime_delete_fails(delete_fakes):
    """The IAM reclaim must not be skipped by a runtime failure. The role is the artifact that
    leaks ACCOUNT-GLOBALLY and blocks a re-materialize under the same name (D-A2), so it is
    strictly worse to leave behind than the runtime is."""
    f = delete_fakes
    f.agent.agent_arns = {"dev": DEV_ARN}
    f.identity.delete_runtime_raises = RuntimeError("AccessDenied")

    f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    # EVERY candidate is still attempted, not just the first: a runtime failure must not cut the
    # reclaim short at any point. Order-free (`set`) — the assertion is about coverage, and the
    # order the names come back in is pinned by the dedicated ordering test.
    assert _deleted_roles(f) == {_exec_role("dev"), LEGACY_EXEC_ROLE}


def test_preview_reports_the_runtime_PRESENT_when_ANY_stage_runtime_survives(delete_fakes):
    """The preview offers ONE `runtime` line-item for what may be N runtimes (the shape T2
    left in place). Aggregating honestly means `present` if ANY of them is still there — a
    `gone` while prod's runtime is live would let an operator uncheck the box and leak it.

    ORDER-INDEPENDENT ON PURPOSE, and the setup is chosen so it stays that way while still
    proving the MAP is what gets probed. The first cut left the fixture scalar naming a THIRD
    runtime the map does not, so the real probe list was three ARNs and
    `sorted(probed) == [DEV, PROD]` held only because `dict` iteration happened to reach the live
    runtime second and short-circuit before the scalar — reversing the literal map order alone
    reddened it. Now the scalar mirrors a stage the map already names (C-A2's dual write, as the
    sibling GONE test does) so the candidate set is exactly the two mapped ARNs in any order, and
    the LIVE one is the stage the scalar does NOT name — so an implementation that probed only
    the scalar could never reach it, whichever order the map yields.

    Every assertion below holds under BOTH map orders. In particular there is deliberately NO
    assertion on the probe COUNT or the LAST ARN probed here: those hold only if the loop
    short-circuits, and under a non-short-circuiting implementation they flip with the dict order
    — the very dependency this test is being repaired for. The short-circuit is pinned
    order-freely by the sibling test below instead."""
    f = delete_fakes
    f.agent.agent_arns = {"dev": DEV_ARN, "prod": PROD_ARN}
    f.agent.agent_arn = PROD_ARN  # C-A2: the scalar mirrors the last stage deployed
    probed = []

    def probe(arn, *, control_client=None):  # E36/T8 kwarg — match the real signature
        probed.append(arn)
        return arn == DEV_ARN  # prod's runtime is torn down; dev is still live

    f.identity.runtime_exists = probe

    prev = f.svc.preview_delete(project_id=P, repo_id=R)

    # `present` wins: one live runtime settles the aggregate.
    assert next(i for i in prev.items if i.item == "runtime").state == "present"
    # The live runtime was actually REACHED — this is what makes `present` evidence rather than a
    # default. It is also the stage the scalar does NOT name, so a scalar-only reader fails here
    # in either order (the gone `prod` can never settle the aggregate for it).
    assert DEV_ARN in probed
    # Only the two runtimes the record names were candidates — the de-duped scalar added no third.
    assert set(probed) <= {DEV_ARN, PROD_ARN}


def test_the_preview_STOPS_probing_once_one_runtime_is_proved_present(delete_fakes):
    """The short-circuit, pinned WITHOUT depending on dict order.

    `present` is settled by the first live runtime, so the probe must not keep calling the
    control plane once it has its answer. Making EVERY runtime live is what makes this
    order-free: a short-circuiting loop probes exactly ONE whichever key comes first, while one
    that probes them all makes three calls in every order. Asserting the count on a map with one
    live entry could not distinguish the two without also fixing which key is visited first."""
    f = delete_fakes
    f.agent.agent_arns = {"dev": DEV_ARN, "prod": PROD_ARN, "uat": UAT_ARN}
    f.agent.agent_arn = UAT_ARN  # C-A2: the scalar mirrors the last stage deployed
    probed = []

    def probe(arn, *, control_client=None):  # E36/T8 kwarg — match the real signature
        probed.append(arn)
        return True  # every runtime is live

    f.identity.runtime_exists = probe

    prev = f.svc.preview_delete(project_id=P, repo_id=R)

    assert next(i for i in prev.items if i.item == "runtime").state == "present"
    assert len(probed) == 1
    assert probed[0] in {DEV_ARN, PROD_ARN, UAT_ARN}


def test_preview_reports_the_runtime_GONE_only_when_EVERY_stage_runtime_is_gone(delete_fakes):
    """The converse. `gone` means "no runtime left to delete", so it must require ALL of them."""
    f = delete_fakes
    f.agent.agent_arns = {"dev": DEV_ARN, "prod": PROD_ARN}
    f.agent.agent_arn = PROD_ARN  # C-A2: the scalar mirrors the last stage deployed
    probed = []

    def probe(arn, *, control_client=None):  # E36/T8 kwarg — match the real signature
        probed.append(arn)
        return False

    f.identity.runtime_exists = probe

    prev = f.svc.preview_delete(project_id=P, repo_id=R)

    # EVERY stage's runtime had to be checked before claiming there is nothing left.
    assert sorted(probed) == sorted([DEV_ARN, PROD_ARN])
    assert next(i for i in prev.items if i.item == "runtime").state == "gone"


def test_preview_reports_UNKNOWN_when_a_probe_raises_and_none_proved_present(delete_fakes, caplog):
    """An AccessDenied probe is AMBIGUOUS — a live runtime behind an IAM/SCP/region misconfig
    returns it too. It must never be read as `gone`. But a raise must not HIDE a runtime another
    probe proved present either: `present` outranks `unknown` outranks `gone`.

    Also pins that the ambiguous-probe log names the RID and NOT the account-bearing ARN, the
    same rule as the delete-failure log — asserted on the emitted record with the account id
    derived from the ARN rather than written as a literal."""
    f = delete_fakes
    f.agent.agent_arns = {"dev": DEV_ARN, "prod": PROD_ARN}
    f.agent.agent_arn = PROD_ARN  # C-A2: the scalar mirrors the last stage deployed

    def probe(arn, *, control_client=None):  # E36/T8 kwarg — match the real signature
        if arn == PROD_ARN:
            raise RuntimeError("AccessDenied")
        return False

    f.identity.runtime_exists = probe

    with caplog.at_level(logging.ERROR, logger="services.project_service"):
        prev = f.svc.preview_delete(project_id=P, repo_id=R)

    assert next(i for i in prev.items if i.item == "runtime").state == "unknown"

    probe_failures = [
        r.getMessage()
        for r in caplog.records
        if r.name == "services.project_service" and "runtime probe failed" in r.getMessage()
    ]
    assert len(probe_failures) == 1
    assert probe_failures[0].endswith(PROD_ARN.rsplit("/", 1)[-1])
    assert DEV_ARN.split(":")[4] not in probe_failures[0]   # the account id, derived not spelled
    assert "arn:" not in probe_failures[0]


# =========================================================================== #
# E28C/T5 (D-C5) — the LANGFUSE materialize step                              #
# =========================================================================== #
# `POST /agents` has provisioned a per-agent Langfuse project + key since E26/T4. `add_repo`
# bypasses that route (it calls `registry.create` directly), so a repo-created agent's envelope
# kept `langfuse_key_secret_name = None` — buildspec.yml:308 read that None, the `LANGFUSE_SECRET_NAME`
# tfvar never arrived, and the container traced into nothing. The Observability tab then showed a
# zero that LOOKED like "no traffic yet" rather than "never wired", for two epics.
#
# The fix is a timeline STEP, not an inline call in `add_repo`: provisioning is a ~30s third-party
# round-trip and `add_repo` is the fast pre-202 half. A step also gets what an inline call cannot —
# a visible success/failure row.


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
        # The REAL provisioner writes the C1 join onto the envelope + persists it via the
        # registry; the fake stands in for exactly that write, with the real name shape.
        agent.langfuse_project_id = "clx-proj-1"
        agent.langfuse_key_secret_name = f"langfuse-agent-{agent.id}-keys"
        agent_seen["agent"] = agent

    langfuse.provision_agent_project.side_effect = provision

    svc = _langfuse_service_fakes(scaffold_dir, langfuse)
    repo = _materialize_one(svc)

    # The SAME provisioning path `POST /agents` schedules — `provision_agent_project`, the
    # method that owns both the project create and the envelope write. Nothing is reimplemented
    # here, and nothing else may be called in its place.
    langfuse.provision_agent_project.assert_called_once()
    agent = agent_seen["agent"]
    # The registry object materialize registered — not some copy built for the call.
    assert agent is svc._registry.create.return_value
    assert agent.langfuse_key_secret_name == f"langfuse-agent-{agent.id}-keys"
    # The step's own row reports it, and the run still terminates on `finalize`.
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
    derived = LangfuseProvisioningService._agent_secret_name(
        SimpleNamespace(_agp_project_name="agp"), agent
    )
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
    # Direction one — its OWN row carries the failure.
    assert steps["provision_langfuse"].status is StepStatus.FAILED
    assert steps["provision_langfuse"].error == "RuntimeError"
    # Direction two — the LATER step still ran, and the record is READY, not failed.
    assert steps["finalize"].status is StepStatus.DONE
    assert repo.cicd_status == "ready"
    assert repo.status == "ready"
    # Everything before it is untouched: this is not a step that half-runs the timeline.
    assert [steps[k].status for k in ("mint_identity", "create_repo", "push_template", "set_repo_vars")] == [
        StepStatus.DONE
    ] * 4


def test_a_NON_langfuse_step_failure_STILL_stops_the_run(scaffold_dir):
    """The best-effort tolerance is scoped to ONE step, and this is the non-vacuity guard for
    that. Widening it to every step would silently convert every materialize failure into a
    `ready` repo — the exact inversion of E25C/T2's contract, and it would still pass the test
    above. `set_repo_vars` failing must leave the record `failed` with `finalize` never run."""
    langfuse = MagicMock(name="LangfuseProvisioningService")
    svc = _langfuse_service_fakes(scaffold_dir, langfuse)
    svc._rollout.set_ci_vars.side_effect = RuntimeError("boom")

    repo = _materialize_one(svc)

    steps = {s.key: s for s in repo.steps}
    assert steps["set_repo_vars"].status is StepStatus.FAILED
    assert steps["provision_langfuse"].status is StepStatus.PENDING  # never reached
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
        langfuse_host="",  # the shipped default — "not configured"
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
    # Nothing was invented on the envelope either: an unprovisioned agent must keep a None field
    # rather than a secret name naming a secret that does not exist (buildspec.yml reads it).
    assert svc._registry.create.return_value.langfuse_key_secret_name is None


def test_langfuse_step_is_a_clean_NO_OP_when_the_provisioner_is_UNWIRED(project_service_fakes):
    """The legacy/never-injected construction, kept alongside the host-empty case above.

    Production does not produce this shape (the route always injects), but the ctor arg defaults to
    ``None`` and every pre-E26 test construction relies on it, so the step must tolerate it rather
    than ``AttributeError`` on a missing collaborator."""
    svc = project_service_fakes
    assert svc._langfuse is None  # the shape under test, stated rather than assumed
    _make_project(svc)

    repo = _materialize_one(svc)

    step = next(s for s in repo.steps if s.key == "provision_langfuse")
    assert step.status is StepStatus.DONE
    assert step.error is None
    assert repo.cicd_status == "ready"


# --------------------------------------------------------------------------- #
# E28C live-fix — RETRYING a best-effort failure must reach "ready" again.
#
# THE DEFECT THIS CLOSES, seen live on a real repo. `retry_materialize` resets every NON-done
# step and leaves done ones done, which is the whole point of resume — and it was airtight while
# every step failure was terminal, because a `failed` step and a `done` finalize could not
# coexist: the run stopped at the failure, so finalize was always still `pending`.
#
# `_BEST_EFFORT_STEPS` (above) created exactly that combination. A Langfuse outage fails the
# `provision_langfuse` ROW, the run CONTINUES, and `finalize` completes — record `ready`. When the
# operator then retries, only `provision_langfuse` is reset, retry flips the record to
# `provisioning`, run_materialize re-runs the step successfully, and then SKIPS the done
# `finalize` — the ONLY writer of `ready`. The repo is stranded at `provisioning` forever, with a
# Complete timeline, and no further retry can help (the 409 guard then refuses it).
# --------------------------------------------------------------------------- #


def test_retrying_a_BEST_EFFORT_failure_lands_the_repo_BACK_AT_READY(scaffold_dir):
    """The live sequence, end to end: a failed best-effort row on a `ready` repo, retried.

    Asserted on the RECORD, not only on the row: a retry that turns the red row green while
    leaving the record at `provisioning` is the stranding, and it is the state a real repo was
    found in. `finalize` is the only writer of `ready`/`cicd_status="ready"`, so retry has to put
    it back in play even though it is `done`."""
    langfuse = MagicMock(name="LangfuseProvisioningService")
    langfuse.provision_agent_project.side_effect = RuntimeError("langfuse unreachable")

    svc = _langfuse_service_fakes(scaffold_dir, langfuse)
    # retry_materialize RE-DERIVES the agent from the registry — serve the SAME record materialize
    # registered, so the retried run provisions the same envelope rather than a fresh mock. The
    # re-derivation reads `framework`/`model_id` off it (rebuilding the stash's agent_config), which
    # a real registry record always carries; the shared fake omits them.
    registered = svc._registry.create.return_value
    registered.framework = VALID_AGENT_CONFIG["framework"]
    registered.model_id = VALID_AGENT_CONFIG["model_id"]
    svc._registry.get.return_value = registered

    # Count finalize runs across BOTH runs: idempotence is the premise of the fix, so it has to be
    # exercised, not assumed.
    finalize_calls = []
    real_finalize = svc._finalize_repo

    def counting_finalize(repo_id, repo_url):
        finalize_calls.append((repo_id, repo_url))
        return real_finalize(repo_id, repo_url)

    svc._finalize_repo = counting_finalize

    repo = _materialize_one(svc)

    # Pre-condition — the E28C combination: a FAILED best-effort row beside a DONE finalize on a
    # record that reads ready. Stated rather than assumed; without it the retry below proves nothing.
    steps = {s.key: s for s in repo.steps}
    assert steps["provision_langfuse"].status is StepStatus.FAILED
    assert steps["finalize"].status is StepStatus.DONE
    assert repo.status == "ready"
    assert len(finalize_calls) == 1

    # Langfuse comes back; the operator retries.
    langfuse.provision_agent_project.side_effect = None
    retried = svc.retry_materialize(repo.id)
    assert retried.status == "provisioning"  # in-flight badge, as retry always writes
    svc.run_materialize(repo.id)

    final = svc.get_repo(repo.id)
    after = {s.key: s for s in final.steps}
    assert after["provision_langfuse"].status is StepStatus.DONE
    assert after["provision_langfuse"].error is None
    # THE ASSERTION THAT FAILED before the fix: finalize was skipped as done, so nothing wrote the
    # success values back and the repo sat at "provisioning" with every row green.
    assert final.status == "ready"
    assert final.cicd_status == "ready"
    assert all(s.status is StepStatus.DONE for s in final.steps)
    # finalize ran a SECOND time — the idempotent re-run that produced the ready above.
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
    assert all(s.status is StepStatus.DONE for s in repo.steps)  # the shape under test

    with pytest.raises(ProjectError) as err:
        svc.retry_materialize(repo.id)
    assert err.value.kind == "nothing_to_retry"

    # Nothing was reset, and the record still reads terminal-success.
    after = svc.get_repo(repo.id)
    assert all(s.status is StepStatus.DONE for s in after.steps)
    assert after.status == "ready"
    assert after.cicd_status == "ready"


# =========================================================================== #
# E28C/T5 (D-C5) — the IAM reclaim stops LYING                                #
# =========================================================================== #
# `_reclaim_exec_role` logged AccessDenied and returned, and the cascade reported `deleted` on
# every item. Two independent bugs conspired: the ECS task role's `iam:DeleteRole` grant is scoped
# `role/{prefix}-ecr-push-*`, which can never match `{agent}-{stage}-agentcore-exec`, so the live
# answer was ALWAYS AccessDenied — and the code read that as success. Six roles leaked with a clean
# teardown report on every one. The grant is widened (modules/ecs/main.tf) and the swallow is
# replaced by a REPORTED, still-non-blocking `exec_role` item.


def test_a_DENIED_reclaim_is_REPORTED_as_a_failed_exec_role_item(delete_fakes):
    """The honesty half. A denied reclaim must be visible on the cascade result, because the
    operator is the only thing that can clean up an account-global name AGP could not delete —
    and they cannot act on a report that says `deleted`."""
    f = delete_fakes
    f.iam.delete_role.side_effect = _iam_client_error("AccessDenied", "DeleteRole")

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    item = next(i for i in res.items if i.item == "exec_role")
    assert item.outcome == "failed"
    # The ROLE NAME is the actionable fact — it is what the operator types into the console.
    assert LEGACY_EXEC_ROLE in item.reason
    assert "ClientError" in item.reason


def test_a_DENIED_reclaim_still_removes_the_RECORD(delete_fakes):
    """The non-blocking half, and it is the reason `exec_role` is a separate item rather than a
    raise inside the runtime step. `failed` normally BLOCKS the record delete (the row is kept for
    retry) — but a retry cannot fix a missing IAM grant, so gating the row on this would trap
    every row behind a leaked role while the runtime, repo, image and identity are all gone. So
    `exec_role` is excluded from the record-gating predicate, exactly like `langfuse`."""
    f = delete_fakes
    f.iam.delete_role.side_effect = _iam_client_error("AccessDenied", "DeleteRole")

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    assert next(i for i in res.items if i.item == "exec_role").outcome == "failed"
    assert next(i for i in res.items if i.item == "record").outcome == "deleted"
    assert res.record_removed is True
    assert f.svc._get_repo(R) is None


def test_a_denied_POLICY_delete_alone_also_reports_the_exec_role_item(delete_fakes):
    """The policy delete is half the reclaim, and skipping it does not merely leak a policy — IAM
    then refuses `DeleteRole` with `DeleteConflict`, so it leaks the whole role. A denial there is
    the same operator-actionable fact and must report identically."""
    f = delete_fakes
    f.iam.delete_role_policy.side_effect = _iam_client_error("AccessDenied", "DeleteRolePolicy")

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    item = next(i for i in res.items if i.item == "exec_role")
    assert item.outcome == "failed"
    assert LEGACY_EXEC_ROLE in item.reason


def test_the_exec_role_reason_LEAKS_NOTHING_from_the_exception_BODY(delete_fakes):
    """THE TOKEN-LEAK FENCE (E28B), applied to a message this task newly SURFACES.

    `reason` lands on a read model the console renders, and an AWS error message is arbitrary
    provider text — a request id, an ARN carrying the account id, or, on a wrapper exception, a
    credential. The rule the whole cascade already follows is that a failure reason carries only
    facts THIS code authored: the role name it derived, and `type(err).__name__`. Nothing is read
    out of the exception body, which is why this asserts on a planted secret AND on the account id
    rather than merely eyeballing the format."""
    f = delete_fakes
    f.iam.delete_role.side_effect = ClientError(
        {
            "Error": {
                "Code": "AccessDenied",
                "Message": (
                    f"User: {AGENT_ARN} is not authorized to perform iam:DeleteRole "
                    "— token ghp_SECRET123"
                ),
            },
            "ResponseMetadata": {"RequestId": "req-abc-123"},
        },
        "DeleteRole",
    )

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    reason = next(i for i in res.items if i.item == "exec_role").reason
    assert "ghp_SECRET123" not in reason
    assert "req-abc-123" not in reason
    assert AGENT_ARN.split(":")[4] not in reason  # the account id, derived not spelled
    assert "arn:" not in reason
    # Nothing from the body reaches the whole serialized result either.
    assert "ghp_SECRET123" not in res.model_dump_json()


def test_a_SUCCESSFUL_reclaim_reports_deleted_and_names_no_role(delete_fakes):
    """The success path is unchanged and must stay quiet. A `reason` on a succeeded item would put
    a role name on a green row for no reason, and `NoSuchEntity` — the idempotent already-gone
    state every second delete and every pre-module repo takes — is success, not a leak."""
    f = delete_fakes
    f.iam.delete_role_policy.side_effect = _iam_client_error("NoSuchEntity", "DeleteRolePolicy")
    f.iam.delete_role.side_effect = _iam_client_error("NoSuchEntity", "DeleteRole")

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    item = next(i for i in res.items if i.item == "exec_role")
    assert item.outcome == "deleted"
    assert item.reason is None


def test_ONE_denied_stage_role_is_reported_WITHOUT_hiding_the_reclaimed_ones(delete_fakes):
    """Per-role tolerance survives the honesty change, and the report names WHICH role survived.

    An agent owns one role per stage. A denial on one must still attempt the rest (a partial
    reclaim beats leaking all of them) AND must name the one that leaked — "exec_role: failed"
    with no name would send the operator hunting through IAM for which of three it was."""
    f = delete_fakes
    f.agent.agent_arns = {"dev": DEV_ARN, "prod": PROD_ARN}
    f.iam.delete_role.side_effect = [
        _iam_client_error("AccessDenied", "DeleteRole"),
        None,
        None,
    ]

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    # All three were still ATTEMPTED.
    assert _deleted_roles(f) == {_exec_role("dev"), _exec_role("prod"), LEGACY_EXEC_ROLE}
    item = next(i for i in res.items if i.item == "exec_role")
    assert item.outcome == "failed"
    assert _exec_role("dev") in item.reason
    # And it does not smear the two that SUCCEEDED into the failure message.
    assert _exec_role("prod") not in item.reason
    assert LEGACY_EXEC_ROLE not in item.reason
    assert res.record_removed is True


def test_DESELECTING_the_runtime_reports_exec_role_as_SKIPPED_not_deleted(delete_fakes):
    """The reclaim rides the runtime selection, so an operator keeping the runtime keeps its role
    (a runtime without its exec role cannot pull images or write logs). The item must then read
    `skipped` — never silently absent, and never `deleted`, because a role that is still there is
    exactly the thing this item exists to be honest about."""
    f = delete_fakes

    res = f.svc.delete_repo(
        project_id=P, repo_id=R, selection=RepoDeleteSelection(runtime=False)
    )

    assert f.iam.mock_calls == []
    item = next(i for i in res.items if i.item == "exec_role")
    assert item.outcome == "skipped"


def test_an_agent_with_NO_NAME_reports_exec_role_skipped_rather_than_deleted(delete_fakes):
    """No name ⇒ no derivable role name ⇒ nothing was attempted, so `deleted` would be a claim
    about a call that never happened. This is the gap `_delete_runtime`'s docstring names: a
    second delete attempt after the registry entry is gone leaves the role to be removed by hand,
    and the cascade must say so rather than report a teardown it did not perform."""
    f = delete_fakes
    f.agent.name = None

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    assert f.iam.mock_calls == []
    item = next(i for i in res.items if i.item == "exec_role")
    assert item.outcome == "skipped"
    assert next(i for i in res.items if i.item == "runtime").outcome == "deleted"
    assert res.record_removed is True


def test_the_exec_role_item_is_reported_even_when_the_RUNTIME_delete_FAILS(delete_fakes):
    """A runtime failure must not swallow the reclaim's own report. The runtime item legitimately
    fails (a surviving runtime is live and billing, so the row IS kept for retry) — but the
    exec_role line still has to state what happened to the role, which was reclaimed before the
    raise precisely because it leaks account-globally."""
    f = delete_fakes
    f.agent.agent_arns = {"dev": DEV_ARN}
    f.identity.delete_runtime_raises = RuntimeError("AccessDenied")

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    assert next(i for i in res.items if i.item == "runtime").outcome == "failed"
    assert next(i for i in res.items if i.item == "exec_role").outcome == "deleted"
    assert _deleted_roles(f) == {_exec_role("dev"), LEGACY_EXEC_ROLE}


# =========================================================================== #
# E36/T8 (item 1) — CROSS-ACCOUNT teardown stops reporting a leak as success   #
# =========================================================================== #
# The defect, precisely: every teardown client was built from the backend's AMBIENT
# ECS-task credentials, which live in the CONTROL-PLANE account. A tenant stage carrying a
# `deploy_role_arn` puts its runtime and its exec role in the TENANT's account, so the
# teardown never addressed them — `get_agent_runtime` answered ResourceNotFoundException in
# the control-plane account and `delete_role` answered NoSuchEntity there, and BOTH of those
# are the idempotent already-done state. So the cascade reported `deleted` on a runtime that
# kept billing and an account-global role that kept blocking re-materialization. Not a
# swallowed error: a truthful answer to the wrong question.
#
# Two halves are pinned here. (1) The teardown calls run under the ASSUMED tenant role
# (`services.tenant_credentials.stage_client`). (2) When the assume FAILS the cascade says so
# — `assume_role_failed: <safe reason>` — and NEVER `deleted`; while a NotFound raised under
# the CORRECT (assumed) account is still the idempotent success it always was, because there
# the answer is about the right account.

CROSS_ACCOUNT_ROLE = "arn:aws:iam::210987654321:role/agp-deployment-acme-dev"
CROSS_ACCOUNT_ROLE_NAME = "agp-deployment-acme-dev"
PROD_ROLE = "arn:aws:iam::310987654321:role/agp-deployment-acme-prod"


class _FakeStageClientSeam:
    """The `stage_client` seam, recorded.

    Hands back a DISTINCT double per (service, deploy_role_arn) pair, which is what makes
    "ran under the assumed client" able to fail: a seam returning one shared mock could not
    tell a per-stage assume from a single ambient client used N times.
    """

    def __init__(self):
        self.calls = []  # (service_name, deploy_role_arn, session_suffix)
        self.clients = {}  # (service_name, deploy_role_arn) -> MagicMock
        self.raise_for = set()  # deploy_role_arns whose assume must fail

    def __call__(self, service_name, cfg, *, session_suffix):
        arn = getattr(cfg, "deploy_role_arn", "") if cfg is not None else ""
        self.calls.append((service_name, arn, session_suffix))
        if arn in self.raise_for:
            raise TenantCredentialsError(f"{arn.rsplit('/', 1)[-1]} (ClientError)")
        return self.clients.setdefault((service_name, arn), MagicMock(name=f"{service_name}"))

    def client(self, service_name, deploy_role_arn):
        """The double a given (service, role) pair was served — None if never asked for."""
        return self.clients.get((service_name, deploy_role_arn))


def _arm_tenant(f, stages):
    """Give the fixture's service a tenant whose ``stages`` map is ``stages`` + the recorded
    seam. ``stages`` is ``{stage: deploy_role_arn}`` ("" ⇒ deploy-in-place)."""
    tenant = SimpleNamespace(
        id="default",
        stages={
            stage: TenantStageConfig(
                account_id="210987654321", region="eu-west-1", deploy_role_arn=arn
            )
            for stage, arn in stages.items()
        },
    )
    f.svc._tenants = SimpleNamespace(get=lambda tenant_id: tenant)
    seam = _FakeStageClientSeam()
    f.svc._stage_client = seam
    return seam


def _exec_role_reason(res):
    return next(i for i in res.items if i.item == "exec_role").reason


# -- the exec-role reclaim, under the assumed role --------------------------- #


def test_a_cross_account_stage_reclaims_its_role_under_the_ASSUMED_client(delete_fakes):
    """The fix. The role named `{agent}-dev-agentcore-exec` exists in the TENANT's account,
    so the two IAM calls must be made by a client built from that account's temporary
    credentials — not by the ambient control-plane client, which would answer NoSuchEntity
    about a role it cannot see."""
    f = delete_fakes
    f.agent.agent_arns = {"dev": DEV_ARN}
    f.agent.agent_arn = DEV_ARN
    seam = _arm_tenant(f, {"dev": CROSS_ACCOUNT_ROLE})

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    assert ("iam", CROSS_ACCOUNT_ROLE) in [(s, a) for s, a, _ in seam.calls]
    assumed = seam.client("iam", CROSS_ACCOUNT_ROLE)
    assumed.delete_role.assert_called_once_with(RoleName=_exec_role("dev"))
    # ...and the AMBIENT client was never asked to delete the stage-scoped role.
    assert _exec_role("dev") not in _deleted_roles(f)
    assert next(i for i in res.items if i.item == "exec_role").outcome == "deleted"


def test_the_assume_session_name_identifies_the_agent_being_torn_down(delete_fakes):
    """`RoleSessionName` is what a tenant's CloudTrail shows them, so it has to say which
    agent's teardown opened the session — an opaque session id makes a cross-account delete
    unattributable in the only account that can audit it."""
    f = delete_fakes
    f.agent.agent_arns = {"dev": DEV_ARN}
    f.agent.agent_arn = DEV_ARN  # C-A2: the scalar mirrors the stage deployed last
    seam = _arm_tenant(f, {"dev": CROSS_ACCOUNT_ROLE})

    f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    suffixes = {suffix for _, _, suffix in seam.calls}
    assert suffixes, "the seam was never called"
    for suffix in suffixes:
        assert AGENT_ID in suffix
        assert "teardown" in suffix


def test_an_ASSUME_FAILURE_reports_the_exec_role_item_as_assume_role_failed(delete_fakes):
    """THE PINNED CONTRACT. A failed assume means the role was never even addressed, so the
    one thing the report may not say is `deleted`. `assume_role_failed:` is the prefix
    because the operator's next action is different from every other failure here: this is
    not "retry", it is "the platform cannot reach that account — grant the trust or reclaim
    by hand"."""
    f = delete_fakes
    f.agent.agent_arns = {"dev": DEV_ARN}
    f.agent.agent_arn = DEV_ARN  # C-A2: the scalar mirrors the stage deployed last
    seam = _arm_tenant(f, {"dev": CROSS_ACCOUNT_ROLE})
    seam.raise_for.add(CROSS_ACCOUNT_ROLE)

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    item = next(i for i in res.items if i.item == "exec_role")
    assert item.outcome != "deleted"
    assert item.outcome == "failed"
    assert item.reason.startswith("assume_role_failed: ")
    # The role NAME is the actionable fact (what the operator types into IAM).
    assert CROSS_ACCOUNT_ROLE_NAME in item.reason


def test_an_assume_failure_reason_carries_NO_ACCOUNT_ID_and_no_ARN(delete_fakes):
    """THE TOKEN-LEAK FENCE, applied to a string this task newly surfaces. A
    `deploy_role_arn` CONTAINS the tenant's 12-digit account id, and `reason` renders in the
    console and lands in the logs — where a hard project rule bans an account id outright."""
    f = delete_fakes
    f.agent.agent_arns = {"dev": DEV_ARN}
    f.agent.agent_arn = DEV_ARN  # C-A2: the scalar mirrors the stage deployed last
    seam = _arm_tenant(f, {"dev": CROSS_ACCOUNT_ROLE})
    seam.raise_for.add(CROSS_ACCOUNT_ROLE)

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    reason = _exec_role_reason(res)
    assert "210987654321" not in reason
    assert "arn:" not in reason
    assert "210987654321" not in res.model_dump_json()


def test_a_NoSuchEntity_under_the_ASSUMED_account_stays_idempotent_deleted(delete_fakes):
    """The other half of honesty, and the reason this is not simply "report failure when a
    deploy role exists". NoSuchEntity from the account that OWNS the role is the genuine
    already-done state that a second delete attempt, a pre-module repo and a half-failed
    provision all take. Once the question is asked in the right account the answer is
    trustworthy again, so it stays `deleted`."""
    f = delete_fakes
    f.agent.agent_arns = {"dev": DEV_ARN}
    f.agent.agent_arn = DEV_ARN  # C-A2: the scalar mirrors the stage deployed last
    seam = _arm_tenant(f, {"dev": CROSS_ACCOUNT_ROLE})
    assumed = seam(
        "iam",
        SimpleNamespace(deploy_role_arn=CROSS_ACCOUNT_ROLE, region="eu-west-1"),
        session_suffix="pre-arm",
    )
    assumed.delete_role_policy.side_effect = _iam_client_error("NoSuchEntity", "DeleteRolePolicy")
    assumed.delete_role.side_effect = _iam_client_error("NoSuchEntity", "DeleteRole")

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    item = next(i for i in res.items if i.item == "exec_role")
    assert item.outcome == "deleted"
    assert item.reason is None
    # Discriminating: the NoSuchEntity has to come from the ASSUMED client. An ambient
    # MagicMock would answer "success" to anything, so this must also prove WHO was asked.
    assumed.delete_role.assert_any_call(RoleName=_exec_role("dev"))
    assert _exec_role("dev") not in _deleted_roles(f)


def test_an_AccessDenied_under_the_assumed_account_is_still_a_plain_not_reclaimed(delete_fakes):
    """An assume that WORKED followed by a denied delete is a different fact from an assume
    that failed: the platform reached the account and IAM refused the call. It keeps the
    pre-existing `not reclaimed:` wording, so `assume_role_failed:` stays diagnostic of the
    credential hop alone."""
    f = delete_fakes
    f.agent.agent_arns = {"dev": DEV_ARN}
    f.agent.agent_arn = DEV_ARN  # C-A2: the scalar mirrors the stage deployed last
    seam = _arm_tenant(f, {"dev": CROSS_ACCOUNT_ROLE})
    assumed = seam(
        "iam",
        SimpleNamespace(deploy_role_arn=CROSS_ACCOUNT_ROLE, region="eu-west-1"),
        session_suffix="pre-arm",
    )
    assumed.delete_role.side_effect = _iam_client_error("AccessDenied", "DeleteRole")

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    reason = _exec_role_reason(res)
    assert reason.startswith("not reclaimed: ")
    assert "assume_role_failed" not in reason


def test_an_assume_failure_for_ONE_stage_still_reclaims_the_OTHER(delete_fakes):
    """PER-ROLE TOLERANCE, preserved across the new failure mode. The reclaim loop's
    documented invariant is that one stage's failure never strands the next; raising out of
    the loop on the first bad assume would abandon a role the platform CAN delete, leaking it
    for a reason unrelated to it."""
    f = delete_fakes
    f.agent.agent_arns = {"dev": DEV_ARN, "prod": PROD_ARN}
    f.agent.agent_arn = PROD_ARN
    seam = _arm_tenant(f, {"dev": CROSS_ACCOUNT_ROLE, "prod": PROD_ROLE})
    seam.raise_for.add(CROSS_ACCOUNT_ROLE)

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    # prod's role WAS reclaimed, under prod's own assumed client...
    seam.client("iam", PROD_ROLE).delete_role.assert_any_call(RoleName=_exec_role("prod"))
    # ...and the report still names the credential failure rather than hiding it behind the
    # success, because a partially-torn-down teardown that reads clean is the whole defect.
    item = next(i for i in res.items if i.item == "exec_role")
    assert item.outcome == "failed"
    assert item.reason.startswith("assume_role_failed: ")
    assert CROSS_ACCOUNT_ROLE_NAME in item.reason


def test_the_LEGACY_unscoped_role_is_reclaimed_AMBIENT_never_under_a_guessed_stage(delete_fakes):
    """The pre-T1b un-scoped name belongs to no stage, so there is no `deploy_role_arn` to
    resolve for it. Picking a stage's role to assume with would attempt the delete in an
    account chosen by coin-flip; the honest client is the ambient one, which is also where a
    pre-cross-account deployment actually put it."""
    f = delete_fakes
    f.agent.agent_arns = {"dev": DEV_ARN}
    f.agent.agent_arn = DEV_ARN  # C-A2: the scalar mirrors the stage deployed last
    seam = _arm_tenant(f, {"dev": CROSS_ACCOUNT_ROLE})

    f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    assert LEGACY_EXEC_ROLE in _deleted_roles(f)  # the AMBIENT client's calls
    assumed_names = [
        c.kwargs.get("RoleName")
        for c in seam.client("iam", CROSS_ACCOUNT_ROLE).delete_role.call_args_list
    ]
    assert LEGACY_EXEC_ROLE not in assumed_names


def test_an_UNKNOWN_STAGE_legacy_record_stays_on_the_AMBIENT_client(delete_fakes):
    """A scalar-only record resolves to the `unknown` placeholder stage, which names no
    tenant stage. It must not be looked up (and must not assume): `unknown` is the record
    admitting it does not know its account."""
    f = delete_fakes
    f.agent.agent_arns = {}  # legacy: scalar only
    seam = _arm_tenant(f, {"dev": CROSS_ACCOUNT_ROLE})

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    assert seam.calls == []
    assert LEGACY_EXEC_ROLE in _deleted_roles(f)
    assert next(i for i in res.items if i.item == "exec_role").outcome == "deleted"


def test_a_stage_with_NO_deploy_role_keeps_TODAYS_ambient_behaviour(delete_fakes):
    """Deploy-in-place must be untouched — no assume, no new client, the ambient one the
    whole pre-existing suite injects. A seam that assumed here would need a grant the ECS
    task role does not hold and would turn every single-account teardown into a failure."""
    f = delete_fakes
    f.agent.agent_arns = {"dev": DEV_ARN}
    f.agent.agent_arn = DEV_ARN  # C-A2: the scalar mirrors the stage deployed last
    seam = _arm_tenant(f, {"dev": ""})

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    assert seam.calls == []
    assert _deleted_roles(f) == {_exec_role("dev"), LEGACY_EXEC_ROLE}
    assert next(i for i in res.items if i.item == "exec_role").outcome == "deleted"


# -- the runtime delete, under the assumed role ------------------------------ #


def test_a_cross_account_runtime_is_deleted_under_the_ASSUMED_client(delete_fakes):
    """The runtime half. The control client must be the tenant account's, and it must be the
    SAME client the existence probe uses — see the next test for why that is the whole
    defect."""
    f = delete_fakes
    f.agent.agent_arns = {"dev": DEV_ARN}
    f.agent.agent_arn = DEV_ARN
    seam = _arm_tenant(f, {"dev": CROSS_ACCOUNT_ROLE})

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    assert ("bedrock-agentcore-control", CROSS_ACCOUNT_ROLE) in [
        (s, a) for s, a, _ in seam.calls
    ]
    assumed = seam.client("bedrock-agentcore-control", CROSS_ACCOUNT_ROLE)
    assert f.identity.delete_runtime_clients == [assumed]
    assert f.identity.deleted_runtime_arns == [DEV_ARN]
    assert next(i for i in res.items if i.item == "runtime").outcome == "deleted"


def test_an_ASSUME_FAILURE_reports_the_runtime_item_as_assume_role_failed(delete_fakes):
    """A live, billing runtime reported as `deleted` is the most expensive line in this
    defect. On a failed assume the step must fail with the credential reason — and NOT with
    a generic `TenantCredentialsError` type name, which tells an operator nothing about which
    account they cannot reach."""
    f = delete_fakes
    f.agent.agent_arns = {"dev": DEV_ARN}
    f.agent.agent_arn = DEV_ARN  # C-A2: the scalar mirrors the stage deployed last
    seam = _arm_tenant(f, {"dev": CROSS_ACCOUNT_ROLE})
    seam.raise_for.add(CROSS_ACCOUNT_ROLE)

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    item = next(i for i in res.items if i.item == "runtime")
    assert item.outcome != "deleted"
    assert item.outcome == "failed"
    assert item.reason.startswith("assume_role_failed: ")
    assert CROSS_ACCOUNT_ROLE_NAME in item.reason
    # Nothing was deleted with the wrong credentials.
    assert f.identity.deleted_runtime_arns == []


def test_an_assume_failure_on_the_runtime_KEEPS_the_record_for_retry(delete_fakes):
    """UNLIKE `exec_role`, the runtime step is record-BLOCKING, and it must stay that way
    here: a surviving runtime is live, billing, and about to become untracked. Dropping the
    row would delete the last thing that knows the ARN."""
    f = delete_fakes
    f.agent.agent_arns = {"dev": DEV_ARN}
    f.agent.agent_arn = DEV_ARN  # C-A2: the scalar mirrors the stage deployed last
    seam = _arm_tenant(f, {"dev": CROSS_ACCOUNT_ROLE})
    seam.raise_for.add(CROSS_ACCOUNT_ROLE)

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    assert res.record_removed is False
    assert f.svc._get_repo(R) is not None


def test_a_NotFound_under_the_CORRECT_assumed_account_stays_idempotent_deleted(delete_fakes):
    """The precise inversion of the defect. Under AMBIENT credentials a NotFound meant "not in
    the control-plane account", which said nothing about the tenant's runtime and was
    nonetheless reported as `deleted`. Asked in the account that OWNS the runtime, NotFound is
    the real already-gone state — so it stays the idempotent success it always was."""
    f = delete_fakes
    f.agent.agent_arns = {"dev": DEV_ARN}
    f.agent.agent_arn = DEV_ARN  # C-A2: the scalar mirrors the stage deployed last
    seam = _arm_tenant(f, {"dev": CROSS_ACCOUNT_ROLE})
    f.identity.runtime_exists_return = False  # answered by the ASSUMED client

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    item = next(i for i in res.items if i.item == "runtime")
    assert item.outcome == "deleted"
    assert item.reason is None
    assert f.identity.deleted_runtime_arns == []  # probed gone, nothing to delete
    # The probe itself ran under the assumed client — probing ambient is the original bug.
    assumed = seam.client("bedrock-agentcore-control", CROSS_ACCOUNT_ROLE)
    assert assumed is not None, "the runtime step never assumed the tenant role"
    assert f.identity.exists_clients == [assumed]


def test_the_TF_STATE_delete_stays_on_the_CONTROL_PLANE_client(delete_fakes):
    """Not everything the runtime step reclaims is a tenant-account resource. The Terraform
    state objects live in the CONTROL-PLANE bucket, so assuming the tenant role for them
    would fail against a bucket the tenant never had."""
    f = delete_fakes
    f.agent.agent_arns = {"dev": DEV_ARN}
    f.agent.agent_arn = DEV_ARN  # C-A2: the scalar mirrors the stage deployed last
    _arm_tenant(f, {"dev": CROSS_ACCOUNT_ROLE})

    f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    assert f.s3.listed_prefix is not None, "the control-plane state delete did not run"


def test_a_MIXED_failure_still_names_the_credential_hop(delete_fakes):
    """When one stage's assume fails and another's delete fails on its own, the report has one
    line for two facts. It leads with the credential failure: an ordinary delete failure is a
    retry, an unreachable account is not, and the count still covers both."""
    f = delete_fakes
    f.agent.agent_arns = {"dev": DEV_ARN, "prod": PROD_ARN}
    f.agent.agent_arn = PROD_ARN
    seam = _arm_tenant(f, {"dev": CROSS_ACCOUNT_ROLE, "prod": PROD_ROLE})
    seam.raise_for.add(CROSS_ACCOUNT_ROLE)
    f.identity.delete_runtime_raises = RuntimeError("boom")

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    item = next(i for i in res.items if i.item == "runtime")
    assert item.outcome == "failed"
    assert item.reason.startswith("assume_role_failed: ")
    assert res.record_removed is False


# -- the delete PREVIEW must not undo the fix -------------------------------- #


def test_the_PREVIEW_never_reports_a_cross_account_runtime_as_GONE(delete_fakes):
    """The preview is what DRIVES the cascade: a `gone` runtime is presented as
    already-deleted and UNCHECKED, so an ambient probe answering NotFound in the wrong
    account would have the operator skip the runtime step entirely — leaking it with no
    report at all, which is the defect wearing the modal's clothes. A credential failure
    takes the existing ambiguous branch: `unknown`, which the frontend treats as
    selectable+checked. `gone` is never inferred from a question asked in the wrong place."""
    f = delete_fakes
    f.agent.agent_arns = {"dev": DEV_ARN}
    f.agent.agent_arn = DEV_ARN  # C-A2: the scalar mirrors the stage deployed last
    seam = _arm_tenant(f, {"dev": CROSS_ACCOUNT_ROLE})
    seam.raise_for.add(CROSS_ACCOUNT_ROLE)

    preview = f.svc.preview_delete(project_id=P, repo_id=R)

    assert next(i for i in preview.items if i.item == "runtime").state == "unknown"


def test_the_PREVIEW_probes_under_the_ASSUMED_client(delete_fakes):
    """And when the assume works, the answer is about the right account."""
    f = delete_fakes
    f.agent.agent_arns = {"dev": DEV_ARN}
    f.agent.agent_arn = DEV_ARN  # C-A2: the scalar mirrors the stage deployed last
    seam = _arm_tenant(f, {"dev": CROSS_ACCOUNT_ROLE})

    preview = f.svc.preview_delete(project_id=P, repo_id=R)

    assert next(i for i in preview.items if i.item == "runtime").state == "present"
    assumed = seam.client("bedrock-agentcore-control", CROSS_ACCOUNT_ROLE)
    assert assumed is not None, "the preview never assumed the tenant role"
    assert f.identity.exists_clients == [assumed]


def test_a_NON_credential_seam_failure_never_ESCAPES_the_exec_role_item(delete_fakes):
    """`_delete_exec_role` NEVER RAISES, and that invariant is load-bearing: its caller sits
    OUTSIDE `_run_step`, so an escape would 500 the whole delete route — losing the github,
    image, runtime and identity line-items along with it, and leaving the operator with no
    report of a teardown that mostly SUCCEEDED. A seam failure that is not a credential
    failure (a bad region, a client-construction error) is reported as an ordinary surviving
    role instead of allowed out."""
    f = delete_fakes
    f.agent.agent_arns = {"dev": DEV_ARN}
    f.agent.agent_arn = DEV_ARN
    _arm_tenant(f, {"dev": CROSS_ACCOUNT_ROLE})

    def boom(service_name, cfg, *, session_suffix):
        raise ValueError("invalid region")

    f.svc._stage_client = boom

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    item = next(i for i in res.items if i.item == "exec_role")
    assert item.outcome == "failed"
    assert item.reason == f"not reclaimed: {_exec_role('dev')} (ValueError)"
    # ...and every other item still reported.
    assert {i.item for i in res.items} >= {"github", "image", "runtime", "identity", "record"}


def test_a_NON_credential_seam_failure_makes_the_PREVIEW_unknown_not_a_500(delete_fakes):
    """The read-only probe has the same duty: an unreachable delete modal is strictly worse
    than an `unknown` row, which the frontend already treats as selectable+checked."""
    f = delete_fakes
    f.agent.agent_arns = {"dev": DEV_ARN}
    f.agent.agent_arn = DEV_ARN
    _arm_tenant(f, {"dev": CROSS_ACCOUNT_ROLE})

    def boom(service_name, cfg, *, session_suffix):
        raise ValueError("invalid region")

    f.svc._stage_client = boom

    preview = f.svc.preview_delete(project_id=P, repo_id=R)

    assert next(i for i in preview.items if i.item == "runtime").state == "unknown"


# --------------------------------------------------------------------------- #
# E36/T8 FIX ROUND 1 — the three findings the reviewers proved the first pass
# left open, each of which lets a live tenant-account resource read as `deleted`.
# --------------------------------------------------------------------------- #

# -- I-Q3: PER-STAGE client isolation on the DESTRUCTIVE path ---------------- #
#
# The first pass proved "the call ran under SOME assumed client" — every discriminating
# assertion was single-stage. That leaves the highest-blast-radius mistake in the design
# untested: a client cache keyed by TENANT rather than by (tenant, STAGE), or a
# `_stage_control(...)` hoisted out of the per-target loop. Either issues prod's
# DeleteAgentRuntime under DEV's credentials, where it answers NotFound, which the cascade
# reads as idempotent success — a live prod runtime reported `deleted` and its ARN then
# dropped with the row. Both tests below fail under exactly that refactor.


def test_TWO_cross_account_stages_each_delete_under_their_OWN_assumed_client(delete_fakes):
    """Cross-account confusion is the one mistake here that is irreversible.

    Asserted PAIRWISE (arn ↔ client), not as a set of clients: "both clients were used" is
    satisfied by a loop that swapped them."""
    f = delete_fakes
    f.agent.agent_arns = {"dev": DEV_ARN, "prod": PROD_ARN}
    f.agent.agent_arn = PROD_ARN  # C-A2: the scalar mirrors the last stage deployed
    seam = _arm_tenant(f, {"dev": CROSS_ACCOUNT_ROLE, "prod": PROD_ROLE})

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    dev = seam.client("bedrock-agentcore-control", CROSS_ACCOUNT_ROLE)
    prod = seam.client("bedrock-agentcore-control", PROD_ROLE)
    assert dev is not None and prod is not None, "a stage never assumed its tenant role"
    assert dev is not prod, "the two accounts' clients are the same object"
    # A dict, so the pairing is asserted order-freely (the loop walks `agent_arns`, whose
    # order is not a contract — see `_deleted_roles`).
    assert dict(zip(f.identity.deleted_runtime_arns, f.identity.delete_runtime_clients)) == {
        DEV_ARN: dev,
        PROD_ARN: prod,
    }
    # The PROBE too: it is where a NotFound becomes `deleted`, so a probe under the wrong
    # account's client is the whole defect even when the delete is correctly routed.
    assert dict(zip(f.identity.probed_runtime_arns, f.identity.exists_clients)) == {
        DEV_ARN: dev,
        PROD_ARN: prod,
    }
    assert next(i for i in res.items if i.item == "runtime").outcome == "deleted"


def test_TWO_cross_account_stages_each_reclaim_their_role_under_their_OWN_client(delete_fakes):
    """The IAM half of the same property. An exec role is ACCOUNT-GLOBAL, so a DeleteRole
    issued in the wrong tenant's account answers NoSuchEntity — which this cascade reads as the
    idempotent already-done state and reports `deleted`, while the real role keeps blocking
    re-materialization in the account that holds it."""
    f = delete_fakes
    f.agent.agent_arns = {"dev": DEV_ARN, "prod": PROD_ARN}
    f.agent.agent_arn = PROD_ARN  # C-A2: the scalar mirrors the last stage deployed
    seam = _arm_tenant(f, {"dev": CROSS_ACCOUNT_ROLE, "prod": PROD_ROLE})

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    dev_iam = seam.client("iam", CROSS_ACCOUNT_ROLE)
    prod_iam = seam.client("iam", PROD_ROLE)
    assert dev_iam is not None and prod_iam is not None
    assert dev_iam is not prod_iam

    def deleted_by(client):
        return [c.kwargs.get("RoleName") for c in client.delete_role.call_args_list]

    # EXACT lists, both directions: dev's client saw dev's role and NOTHING else, so a shared
    # or mis-keyed client (which would show both names on one double) fails here.
    assert deleted_by(dev_iam) == [_exec_role("dev")]
    assert deleted_by(prod_iam) == [_exec_role("prod")]
    # ...and the legacy un-scoped name, which belongs to no stage, stayed ambient.
    assert LEGACY_EXEC_ROLE in _deleted_roles(f)
    assert _exec_role("dev") not in _deleted_roles(f)
    assert _exec_role("prod") not in _deleted_roles(f)
    assert next(i for i in res.items if i.item == "exec_role").outcome == "deleted"


# -- I-Q2: an UNRESOLVABLE stage must not fall back to the control-plane client - #
#
# `_stage_cfg` used to return None — "use the ambient client" — for three states that are not
# "we do not know the account" but "we could not find out": a raising tenant lookup (a DDB
# throttle), a missing tenant record, and a stage the tenant no longer lists. For a record
# whose stage carries a `deploy_role_arn`, all three then asked the CONTROL-PLANE account,
# which answers ResourceNotFoundException / NoSuchEntity — the idempotent already-done state.
# So a transient store failure during teardown re-manufactured the exact false `deleted` this
# task exists to remove, and did it silently in two of the three cases.
#
# Ambient-BY-DESIGN is untouched: no tenant id, no tenant service, no stage, UNKNOWN_STAGE and
# a stage with an empty `deploy_role_arn` all still use the ambient client (tests above).


def _arm_lookup(f, get):
    """Like `_arm_tenant`, but the caller supplies the tenant LOOKUP itself."""
    f.svc._tenants = SimpleNamespace(get=get)
    seam = _FakeStageClientSeam()
    f.svc._stage_client = seam
    return seam


def _raising_lookup(tenant_id):
    raise RuntimeError("ProvisionedThroughputExceededException")


def _no_tenant_record(tenant_id):
    return None


def _tenant_without_the_stage(tenant_id):
    return SimpleNamespace(
        id="default",
        stages={
            "prod": TenantStageConfig(
                account_id="310987654321", region="eu-west-1", deploy_role_arn=PROD_ROLE
            )
        },
    )


@pytest.mark.parametrize(
    "lookup",
    [
        pytest.param(_raising_lookup, id="the tenant lookup RAISES (a store throttle)"),
        pytest.param(_no_tenant_record, id="the tenant record is MISSING"),
        pytest.param(_tenant_without_the_stage, id="the tenant no longer LISTS the stage"),
    ],
)
def test_an_UNRESOLVABLE_stage_is_reported_failed_and_NEVER_degrades_to_ambient(
    delete_fakes, lookup
):
    """All three states, one property: the item says `failed`, and no call is made with the
    control-plane client whose NotFound would be read as success."""
    f = delete_fakes
    f.agent.agent_arns = {"dev": DEV_ARN}
    f.agent.agent_arn = DEV_ARN  # C-A2: the scalar mirrors the stage deployed last
    seam = _arm_lookup(f, lookup)

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    runtime = next(i for i in res.items if i.item == "runtime")
    assert runtime.outcome != "deleted"
    assert runtime.outcome == "failed"
    assert runtime.reason.startswith("stage_unresolved: ")
    assert "dev" in runtime.reason  # the stage is the actionable fact
    # NOTHING was asked of any client: the resolution fails before the identity call, so the
    # runtime was neither probed nor deleted under the wrong account's credentials.
    assert f.identity.deleted_runtime_arns == []
    assert f.identity.exists_clients == []
    assert seam.calls == []  # never even reached the seam — there was no config to hand it

    # The exec-role half is honest too, and per-ROLE: the tenant-account role is reported as
    # surviving while the legacy un-scoped one (stage None ⇒ ambient BY DESIGN) is reclaimed.
    exec_role = next(i for i in res.items if i.item == "exec_role")
    assert exec_role.outcome == "failed"
    assert "stage_unresolved" in exec_role.reason
    assert _exec_role("dev") in exec_role.reason
    assert _exec_role("dev") not in _deleted_roles(f)  # the ambient client was NOT substituted
    assert LEGACY_EXEC_ROLE in _deleted_roles(f)

    # The runtime step is record-BLOCKING, so the row survives for the retry.
    assert res.record_removed is False
    assert f.svc._get_repo(R) is not None


def test_an_UNRESOLVABLE_stage_makes_the_PREVIEW_unknown_never_GONE(delete_fakes):
    """Same rule on the read-only path, and for the same reason the credential failure takes
    it: `gone` presents the runtime as already-deleted and UNCHECKED, so an operator would
    skip the step entirely. "I could not work out which account to ask" is not "it is not
    there"."""
    f = delete_fakes
    f.agent.agent_arns = {"dev": DEV_ARN}
    f.agent.agent_arn = DEV_ARN  # C-A2: the scalar mirrors the stage deployed last
    _arm_lookup(f, _raising_lookup)

    preview = f.svc.preview_delete(project_id=P, repo_id=R)

    assert next(i for i in preview.items if i.item == "runtime").state == "unknown"
    assert f.identity.exists_clients == []  # never probed the control-plane account


def test_StageUnresolvedError_is_distinct_from_a_credential_failure():
    """Two different operator actions — "the platform cannot get INTO that account" (grant the
    trust) vs "the platform cannot work out WHICH account" (fix the tenant/stage record) — so
    the report prefixes and the exception types are deliberately separate."""
    err = StageUnresolvedError("tenant lookup failed for stage 'dev'")

    assert isinstance(err, Exception)
    assert not isinstance(err, TenantCredentialsError)
    assert err.message == "tenant lookup failed for stage 'dev'"


# -- I-B: a NON-credential client failure must not escape the runtime loop ---- #


def test_a_NON_credential_client_failure_keeps_per_runtime_tolerance_and_the_TF_STATE(
    delete_fakes,
):
    """`stage_client`'s FINAL `boto3.client(...)` sits outside its own try, so a bad region or
    an unknown service raises something that is neither `TenantCredentialsError` nor
    `StageUnresolvedError`. Left unguarded it escaped `_delete_runtime` mid-loop, which lost
    per-runtime tolerance AND skipped the TF-state reclaim that sits after the loop — leaking
    the state objects. The two sibling paths (`_delete_exec_role`, `_probe_runtime`) already
    carried this guard; the runtime delete did not."""
    f = delete_fakes
    f.agent.agent_arns = {"dev": DEV_ARN, "prod": PROD_ARN}
    f.agent.agent_arn = PROD_ARN  # C-A2: the scalar mirrors the last stage deployed
    seam = _arm_tenant(f, {"dev": CROSS_ACCOUNT_ROLE, "prod": PROD_ROLE})

    def boom(service_name, cfg, *, session_suffix):
        arn = getattr(cfg, "deploy_role_arn", "") if cfg is not None else ""
        if service_name == "bedrock-agentcore-control" and arn == CROSS_ACCOUNT_ROLE:
            raise ValueError("invalid region")  # NOT a TenantCredentialsError
        return seam(service_name, cfg, session_suffix=session_suffix)

    f.svc._stage_client = boom

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    runtime = next(i for i in res.items if i.item == "runtime")
    assert runtime.outcome != "deleted"
    assert runtime.outcome == "failed"
    # Per-RUNTIME tolerance: prod was still attempted, under prod's own assumed client.
    assert f.identity.deleted_runtime_arns == [PROD_ARN]
    assert f.identity.delete_runtime_clients == [
        seam.client("bedrock-agentcore-control", PROD_ROLE)
    ]
    # ...and the control-plane TF-state delete, which sits AFTER the loop, still ran.
    assert f.s3.listed_prefix is not None, "the TF-state objects leaked"
    assert res.record_removed is False


# -- I-C: `self._iam is None` no longer gates the cross-account reclaim ------- #


def test_a_MISSING_control_plane_IAM_client_no_longer_SUPPRESSES_a_cross_account_reclaim(
    delete_fakes,
):
    """`self._iam` degrades to None when `boto3.client("iam")` fails to build. Pre-seam that
    was "we cannot do this at all"; post-seam it is only the CONTROL-PLANE client, and whether
    it exists says nothing about assuming into a tenant account. The old gate therefore skipped
    a reclaim that works — and blamed it on `no agent record to derive the role name`, a cause
    that was not the actual one."""
    f = delete_fakes
    f.agent.agent_arns = {"dev": DEV_ARN}
    f.agent.agent_arn = DEV_ARN  # C-A2: the scalar mirrors the stage deployed last
    seam = _arm_tenant(f, {"dev": CROSS_ACCOUNT_ROLE})
    f.svc._iam = None  # the ambient client failed to build

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    # The tenant-account role WAS reclaimed, under its own assumed client.
    seam.client("iam", CROSS_ACCOUNT_ROLE).delete_role.assert_any_call(
        RoleName=_exec_role("dev")
    )
    item = next(i for i in res.items if i.item == "exec_role")
    # The only casualty is the LEGACY un-scoped role, which is genuinely ambient — reported
    # per ROLE with its real cause instead of blanking the whole item.
    assert item.outcome == "failed"
    assert item.reason == f"not reclaimed: {LEGACY_EXEC_ROLE} (no IAM client)"
    assert "no agent record" not in item.reason
    assert item.outcome != "skipped"


def test_NO_AGENT_NAME_is_still_the_only_reason_the_exec_role_item_is_SKIPPED(delete_fakes):
    """The other half of the same fix: `skipped` + `no agent record to derive the role name` is
    now EXACT rather than conflated, so it must still be produced by the state it names."""
    f = delete_fakes
    f.registry.get_return = None  # the registry entry is gone (the second-delete shape)

    res = f.svc.delete_repo(project_id=P, repo_id=R, selection=RepoDeleteSelection())

    item = next(i for i in res.items if i.item == "exec_role")
    assert item.outcome == "skipped"
    assert item.reason == "no agent record to derive the role name"
