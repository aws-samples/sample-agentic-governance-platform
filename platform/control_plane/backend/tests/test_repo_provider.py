"""The portable ``RepoProvider`` seam and its one adapter, in three layers: the Protocol as a
contract (signatures compared parameter by parameter, since a Protocol is not enforced at runtime),
the semantics over ``_FakeGit`` from ``test_project_service`` (reused rather than re-written: it
models per-branch trees and content-based tree shas, the dimensions a more generous fake lacked when
divergent trees shipped three times), and the adapter's actual HTTP over ``httpx.MockTransport``,
where the one-commit claim is a request count only the wire can falsify. The token appears in no
assertion as an expected substring and in several as a forbidden one.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import inspect
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_project_materialize import _FakeGit

from services.github_repo_errors import GitHubRepoError
from services.github_repo_service import GitHubRepoService
from services.repo_provider import RepoProvider, RepoView

_BASE = "https://gh.example"
_TOKEN = "ghs_never_in_a_message"

_THE_METHODS = (
    "create_repo",
    "commit_files",
    "set_ci_vars",
    "ensure_pipeline",
    "read_tree",
    "read_repo",
    "list_repos",
    "commit_files_on_head",
    "read_file",
    "create_branch",
)


def _svc(handler):
    """A service over MockTransport plus an ordered ``(method, path)`` log.

    The log is the ONLY way the one-commit claim is testable: it is a count, not a state."""
    calls: list[tuple[str, str]] = []

    def _wrapped(req: httpx.Request) -> httpx.Response:
        calls.append((req.method, req.url.path))
        return handler(req)

    return GitHubRepoService(client=httpx.Client(transport=httpx.MockTransport(_wrapped))), calls


def test_the_protocol_has_exactly_the_declared_methods():
    """Locked deliberately: a further method is how a provider-specific verb (``create_environment``,
    a GitHub-only concept) creeps back into a portable seam, and every addition so far cleared the
    bar of being implementable on GitHub, GitLab, Bitbucket and Azure DevOps with no premium tier
    and no provider concept in the signature."""
    declared = {
        name for name, member in vars(RepoProvider).items() if not name.startswith("_") and callable(member)
    }
    assert declared == set(_THE_METHODS)


def test_github_repo_service_satisfies_the_protocol_signature_by_signature():
    """Structural conformance, checked where it can actually break.

    ``isinstance`` against a ``runtime_checkable`` Protocol only checks that the NAMES exist —
    it reads no signature at all. So an adapter whose ``commit_files`` took ``paths=`` instead
    of ``files=``, or made ``branch`` positional, would pass an ``isinstance`` assertion and
    fail every portable caller. Parameter names, kinds and defaults are therefore compared
    individually.

    ANNOTATIONS are deliberately NOT compared: ``str | None`` and ``Optional[str]`` are the same
    type, and this module writes the older spelling to match the file it lives in. Comparing the
    strings would fail on a difference that is not one."""
    assert isinstance(GitHubRepoService(), RepoProvider)
    for name in _THE_METHODS:
        want = inspect.signature(getattr(RepoProvider, name))
        got = inspect.signature(getattr(GitHubRepoService, name))
        want_params = list(want.parameters.values())
        got_params = list(got.parameters.values())
        assert [p.name for p in got_params] == [p.name for p in want_params], name
        assert [p.kind for p in got_params] == [p.kind for p in want_params], name
        assert [p.default for p in got_params] == [p.default for p in want_params], name


def test_the_github_shaped_predecessors_are_gone():
    """Each name below was a second write to a repository the provider was already writing to,
    and a callable path back to that shape is how the four-defect race returns; a re-added
    ``create_environment`` would also silently re-widen the App's permission surface. Reads the
    class, not the source text, since a grep is satisfied by a comment or a dead branch."""
    for name in [
        "list_template_repos",
        "generate_from_template",
        "commit_file",
        "create_environment",
        "set_environment_variables",
        "set_repo_variables",
        "set_repo_metadata",
    ]:
        assert getattr(GitHubRepoService, name, None) is None, (
            f"{name} is back. It has no caller — see the module docstring for why it must not "
            f"return: every one of these was a second write racing the provider's own."
        )


def test_the_methods_that_are_NOT_dead_are_still_present():
    """The other half of the fence, so the sweep above cannot become "delete more".

    These are not the GitHub-shaped predecessors: ``create_repo_from_zip`` still pushes template
    and infra repos (``template_rollout_service``), ``delete_repo`` backs the catalog delete and
    the teardown cascade, ``repo_exists`` backs the delete-preview probe, and
    ``set_default_branch``/``delete_branch`` are the trunk adoption. A sweep that took any of them
    would break a live path that no Protocol method covers."""
    for name in [
        "create_repo_from_zip",
        "delete_repo",
        "repo_exists",
        "set_default_branch",
        "delete_branch",
    ]:
        assert callable(getattr(GitHubRepoService, name, None)), f"{name} has a live caller"


def test_the_seam_module_holds_no_credential_and_no_transport():
    """The Protocol module is a CONTRACT, not a client.

    Every method takes ``token`` as a parameter and none can obtain one — which is what keeps an
    adapter from widening its own authority. Asserted over the RAW source (no lowercasing, no
    comment stripping: a normalisation step is how a guard stops seeing what it guards), because
    an import added later is exactly the change a reviewer would otherwise catch by eye."""
    src = Path("src/services/repo_provider.py").read_text()
    assert len(src) > 500, "read the real module, not an empty file"
    for forbidden in ["httpx", "boto3", "requests", "connection_service", "get_bearer_token"]:
        assert forbidden not in src, f"repo_provider reaches a transport/credential seam: {forbidden}"


def test_ensure_pipeline_returns_none_and_issues_no_request():
    """``None`` IS SUCCESS, and a no-op spends no round trip.

    GitHub Actions registers the committed workflow itself, so there is nothing to create. Two
    claims, both load-bearing: the return is ``None`` (a caller that raised on a falsy value
    would break GitHub, GitLab AND Bitbucket while looking correct against Azure DevOps), and NO
    HTTP happens — a "no-op" that still called GitHub would be a latency and a rate-limit cost
    on every materialize, and would fail when the token lacked a scope it never needed."""

    def handler(req):
        raise AssertionError(f"ensure_pipeline must issue no request, sent {req.method} {req.url}")

    svc, calls = _svc(handler)
    assert (
        svc.ensure_pipeline(
            "acme", "r", yaml_path=".github/workflows/build.yml", token=_TOKEN, base_url=_BASE
        )
        is None
    )
    assert calls == []


class _FakeProvider:
    """A ``RepoProvider`` over :class:`_FakeGit`.

    ``commit_files`` is built on ``_FakeGit._commit`` rather than by looping ``write``, which
    would produce N commits for N files and make the one-commit contract untestable here; the
    tree stays per-branch and the tree sha content-derived. ``commit_files`` and ``read_tree``
    carry real behaviour because they are inverses over the same model; ``read_tree`` reads by
    ref (a commit sha), not by branch name, so a mid-read push cannot yield a mixed tree. The
    rest are HTTP shapes, and those are the adapter tests' job.
    """

    def __init__(self):
        self.git = _FakeGit()
        self.repos: list[tuple[str, str, bool]] = []
        self.ci_vars: dict[str | None, dict[str, str]] = {}
        self.commit_messages: list[str] = []
        self.pushes: list[str] = []

    def create_repo(self, org, name, *, private, token, base_url=None):
        self.repos.append((org, name, private))
        return f"https://github.com/{org}/{name}"

    def read_tree(self, org, repo, *, ref, token, base_url=None):
        if not ref:
            raise GitHubRepoError("read_tree requires a ref")
        if ref not in self.git.commits:
            raise GitHubRepoError(f"no such ref '{ref}'")
        return dict(self.git.commits[ref]["tree"])

    def read_repo(self, org, repo, *, token, base_url=None):
        if (org, repo, True) not in self.repos and (org, repo, False) not in self.repos:
            return None
        tip = self.git.branches.get("main", "")
        return RepoView(default_branch="main", head_sha=tip)

    def list_repos(self, org, *, token, base_url=None):
        return [name for (o, name, _private) in self.repos if o == org]

    def commit_files(self, org, repo, files, *, branch, message, token, base_url=None):
        if not files:
            raise GitHubRepoError("refusing to commit an empty file set")
        tree = dict(files)
        if branch in self.git.branches and self.git.tree(branch) == tree:
            return self.git.branches[branch]
        parents = [self.git.branches[branch]] if branch in self.git.branches else []
        sha = self.git._commit(tree, parents)
        self.git.branches[branch] = sha
        self.commit_messages.append(message)
        self.pushes.append(branch)
        return sha

    def commit_files_on_head(
        self, org, repo, files, *, branch, expected_head_sha, message, token, base_url=None
    ):
        from services.github_repo_errors import GitHubRepoConflictError

        if self.git.branches.get(branch) != expected_head_sha:
            raise GitHubRepoConflictError("branch moved")
        tree = {**self.git.tree(branch), **files}
        if tree == self.git.tree(branch):
            return expected_head_sha
        sha = self.git._commit(tree, [expected_head_sha])
        self.git.branches[branch] = sha
        self.commit_messages.append(message)
        self.pushes.append(branch)
        return sha

    def read_file(self, org, repo, path, *, ref, token, base_url=None):
        return self.read_tree(org, repo, ref=ref, token=token).get(path)

    def create_branch(self, org, repo, *, branch, from_branch, token, base_url=None):
        tip = self.git.branches[from_branch or "main"]
        self.git.branches.setdefault(branch, tip)
        return tip

    def set_ci_vars(self, org, repo, variables, *, scope, token, base_url=None):
        self.ci_vars[scope] = {k: v for k, v in variables.items() if v}

    def ensure_pipeline(self, org, repo, *, yaml_path, token, base_url=None):
        return None


_TEMPLATE = {
    "src/main.py": b"# the agent\n",
    ".github/workflows/build.yml": b"name: build\n",
    "requirements.txt": b"strands\n",
}


def test_the_fake_provider_satisfies_the_protocol():
    """The test double is held to the SAME contract as the adapter.

    A double that drifted from the interface would let the semantic tests below pass against a
    shape no real provider has."""
    assert isinstance(_FakeProvider(), RepoProvider)


def test_commit_files_writes_one_commit_for_n_files():
    """N files, ONE commit — the property the whole epic rests on.

    Six writes to a fresh repo is what raced GitHub's asynchronous template copy four separate
    times. One commit also means ONE CI trigger, so a single ``[skip ci]`` suppresses the
    materialize build with no branch filter to keep correct."""
    p = _FakeProvider()
    p.commit_files(
        "acme",
        "r",
        _TEMPLATE,
        branch="main",
        message="chore: initialize from template [skip ci]",
        token=_TOKEN,
    )

    assert len(p.git.commits) == 1
    assert p.git.tree("main") == _TEMPLATE
    assert p.pushes == ["main"]


def test_commit_files_is_a_no_op_when_the_content_is_identical():
    """A RETRIED materialize must converge, not append.

    Identical content is the tree already on the branch, so the second call writes no commit and
    fires no push. Both halves matter: an empty commit would still be a push, and a push on an
    agent repo is a build; a config commit on ``main`` once registered a prod candidate on a repo
    nobody had merged to."""
    p = _FakeProvider()
    first = p.commit_files("acme", "r", _TEMPLATE, branch="main", message="m", token=_TOKEN)
    tree_after_first = p.git.tree_sha("main")

    again = p.commit_files("acme", "r", dict(_TEMPLATE), branch="main", message="m", token=_TOKEN)

    assert again == first
    assert len(p.git.commits) == 1, "a re-run must not add a commit"
    assert p.pushes == ["main"], "a re-run must not push"
    assert p.git.tree_sha("main") == tree_after_first


def test_commit_files_does_commit_when_one_byte_differs():
    """The negative of the test above — without it, "idempotent" is indistinguishable from
    "never writes twice", and a provider that silently dropped every second push would pass."""
    p = _FakeProvider()
    p.commit_files("acme", "r", _TEMPLATE, branch="main", message="m", token=_TOKEN)
    changed = dict(_TEMPLATE, **{"src/main.py": b"# the agent, edited\n"})

    p.commit_files("acme", "r", changed, branch="main", message="m2", token=_TOKEN)

    assert len(p.git.commits) == 2
    assert p.git.tree("main")["src/main.py"] == b"# the agent, edited\n"
    assert p.pushes == ["main", "main"]


def test_commit_files_carries_nothing_forward_from_the_previous_tree():
    """The tree is EXACTLY ``files``, so a template author may DELETE a path.

    Built with no ``base_tree``: a file the new template dropped is gone rather than inherited.
    An inherited path would make AGP the owner of a layout it promises never to inspect."""
    p = _FakeProvider()
    p.commit_files("acme", "r", _TEMPLATE, branch="main", message="m", token=_TOKEN)

    p.commit_files("acme", "r", {"src/main.py": b"# only this\n"}, branch="main", message="m2", token=_TOKEN)

    assert p.git.tree("main") == {"src/main.py": b"# only this\n"}


def test_commit_files_to_one_branch_is_invisible_on_another():
    """Per-branch trees, the dimension the earlier fake lacked.

    A write to `dev` that showed up on `main` is precisely how the divergent-tree defects
    passed their tests. Asserted here so this module inherits the property rather than
    assuming it."""
    p = _FakeProvider()
    p.commit_files("acme", "r", _TEMPLATE, branch="main", message="m", token=_TOKEN)
    p.commit_files(
        "acme", "r", dict(_TEMPLATE, **{"extra.txt": b"x"}), branch="dev", message="m2", token=_TOKEN
    )

    assert "extra.txt" not in p.git.tree("main")
    assert "extra.txt" in p.git.tree("dev")
    assert p.git.tree_sha("main") != p.git.tree_sha("dev")


def test_commit_files_refuses_an_empty_file_set():
    """An empty mapping would build an EMPTY tree — i.e. delete the branch's whole contents.
    A caller with nothing to push never meant that, so it fails closed."""
    p = _FakeProvider()
    with pytest.raises(GitHubRepoError):
        p.commit_files("acme", "r", {}, branch="main", message="m", token=_TOKEN)


def test_read_tree_is_the_exact_INVERSE_of_commit_files():
    """The property the seam turns on: what ``commit_files`` wrote,
    ``read_tree`` gives back — SAME ``dict[path, bytes]``, byte for byte.

    This is why materialize can stop shipping disk bytes: the repo path and the seed path speak
    one shape, so ``read_tree``'s output is a drop-in for ``collect_scaffold_files``'s with no
    adapter in between. Stated over ``_FakeGit`` rather than the wire because it is a claim about
    the two methods' RELATIONSHIP, not about GitHub's encoding — Layer 3 pins the encoding.

    Compared with ``==`` on the whole mapping, not key-by-key: a read that dropped a path, added
    one, or returned ``str`` where ``bytes`` went in must all redden this."""
    p = _FakeProvider()
    sha = p.commit_files("acme", "r", _TEMPLATE, branch="main", message="m", token=_TOKEN)

    back = p.read_tree("acme", "r", ref=sha, token=_TOKEN)

    assert back == _TEMPLATE
    assert all(isinstance(v, bytes) for v in back.values()), "values are bytes, never str"


def test_read_tree_at_an_OLD_ref_does_not_see_a_later_push():
    """``ref`` IS THE POINT: a read pinned to a sha cannot yield a mixed tree.

    Materialize resolves a head sha, then reads at it. If ``read_tree`` tracked the BRANCH
    instead, a template author pushing mid-materialize would have half their old template and
    half their new one land in a customer's repo — a tree that never existed as a commit. The
    older ref must still answer the older bytes."""
    p = _FakeProvider()
    first = p.commit_files("acme", "r", _TEMPLATE, branch="main", message="m", token=_TOKEN)
    second = p.commit_files(
        "acme", "r", {"src/main.py": b"# rewritten\n"}, branch="main", message="m2", token=_TOKEN
    )

    assert first != second
    assert p.read_tree("acme", "r", ref=first, token=_TOKEN) == _TEMPLATE
    assert p.read_tree("acme", "r", ref=second, token=_TOKEN) == {"src/main.py": b"# rewritten\n"}


def test_read_tree_hands_back_a_copy_the_caller_cannot_use_to_edit_history():
    """Materialize passes what it read straight into ``commit_files``, and the reconcile surface
    compares trees. A shared mutable mapping would let one caller's edit change what the next
    read reports — a fake more generous than reality in the other direction."""
    p = _FakeProvider()
    sha = p.commit_files("acme", "r", _TEMPLATE, branch="main", message="m", token=_TOKEN)

    p.read_tree("acme", "r", ref=sha, token=_TOKEN)["src/main.py"] = b"tampered"

    assert p.read_tree("acme", "r", ref=sha, token=_TOKEN) == _TEMPLATE


def test_read_repo_answers_none_for_a_repo_that_is_not_there():
    """``None`` is NOT-FOUND and nothing else. It is the whole basis of the three-state
    reconcile: ``registered_missing`` is a record whose ``read_repo`` answered ``None``. A
    transport or auth failure must RAISE instead, or an outage would read as "the customer's
    templates are gone" and offer to re-create them."""
    p = _FakeProvider()
    assert p.read_repo("acme", "never-created", token=_TOKEN) is None

    p.create_repo("acme", "r", private=True, token=_TOKEN)
    view = p.read_repo("acme", "r", token=_TOKEN)
    assert isinstance(view, RepoView) and view.default_branch == "main"


def test_repo_view_is_frozen_so_a_probe_result_cannot_be_edited_downstream():
    """A ``RepoView`` is what the reconcile surface RENDERS and what adopt writes into a record.
    Frozen because a mutated probe result is a record that disagrees with the provider it came
    from, and the projection idiom (``PullRequestView``) is likewise not a mutable carrier."""
    view = RepoView(default_branch="main", head_sha="abc123")
    untyped: Any = view
    with pytest.raises(dataclasses.FrozenInstanceError):
        untyped.head_sha = "def456"


def _created(name="r", org="acme", html_url=None):
    return {"html_url": html_url or f"https://github.com/{org}/{name}", "owner": {"login": org}}


def test_create_repo_auto_inits_and_forwards_private():
    """``auto_init=True`` is a PRECONDITION of the next call, not a nicety.

    Against a truly empty repo the git-data API answers 409 "Git Repository is empty", so a repo
    created without it cannot receive the ``commit_files`` that immediately follows. ``private``
    is forwarded rather than hardcoded: it is the caller's decision."""

    def handler(req):
        if req.method == "POST" and req.url.path == "/orgs/acme/repos":
            body = json.loads(req.content)
            assert body["auto_init"] is True
            assert body["private"] is False
            assert body["name"] == "r"
            assert req.headers["Authorization"] == f"Bearer {_TOKEN}"
            return httpx.Response(201, json=_created())
        return httpx.Response(404)

    svc, calls = _svc(handler)
    url = svc.create_repo("acme", "r", private=False, token=_TOKEN, base_url=_BASE)

    assert url == "https://github.com/acme/r"
    assert calls == [("POST", "/orgs/acme/repos")]


def test_create_repo_treats_an_already_exists_422_as_a_benign_rerun():
    """``retry_materialize`` re-enters here, so an existing repo must CONVERGE.

    The 422 body describes the rejection and carries no ``html_url``, so the URL is read back —
    the caller PERSISTS what this returns, and a fabricated URL would be a record that lies."""

    def handler(req):
        if req.method == "POST" and req.url.path == "/orgs/acme/repos":
            return httpx.Response(
                422,
                json={
                    "message": "Repository creation failed.",
                    "errors": [{"message": "name already exists on this account"}],
                },
            )
        if req.method == "GET" and req.url.path == "/repos/acme/r":
            return httpx.Response(200, json=_created(html_url="https://github.com/acme/r"))
        return httpx.Response(404)

    svc, calls = _svc(handler)
    assert (
        svc.create_repo("acme", "r", private=True, token=_TOKEN, base_url=_BASE)
        == "https://github.com/acme/r"
    )
    assert ("GET", "/repos/acme/r") in calls


def test_create_repo_fails_closed_on_every_other_422():
    """422 is OVERLOADED on this endpoint. Only "already exists" is benign; a rejected name or
    a quota refusal must still raise, or a materialize proceeds against a repo that is not
    there. The message carries GitHub's own validation string (a safe one) and NEVER the token."""

    def handler(req):
        return httpx.Response(
            422,
            json={
                "message": "Repository creation failed.",
                "errors": [{"message": "name is too long (maximum is 100 characters)"}],
            },
        )

    svc, _ = _svc(handler)
    with pytest.raises(GitHubRepoError) as ei:
        svc.create_repo("acme", "r", private=True, token=_TOKEN, base_url=_BASE)
    text = str(ei.value)
    assert "422" in text and "too long" in text
    assert _TOKEN not in text


def test_create_repo_transport_error_is_safe_and_tokenless():
    def handler(req):
        raise httpx.ConnectError("boom")

    svc, _ = _svc(handler)
    with pytest.raises(GitHubRepoError) as ei:
        svc.create_repo("acme", "r", private=True, token=_TOKEN, base_url=_BASE)
    assert _TOKEN not in str(ei.value)
    assert "boom" not in str(ei.value), "the exception VALUE must not be surfaced"


def _blob_sha(content: bytes) -> str:
    """A CONTENT-derived blob sha, the property real git has and the one the fake must mirror.

    Keyed on the content's own hash, NOT on its length. A length-keyed sha is the specific
    generosity that made this module's path/content assertions vacuous: two different files of
    equal length collided onto one sha, so mangling every path, replacing every blob body with
    garbage, or rotating shas onto the wrong paths all left the tree POST looking correct. Three
    mutants survived because of it.

    ``_FakeGit.tree_sha`` (``test_project_service``) already derives its sha from content for
    exactly this reason — this mirrors that property rather than inventing a scheme."""
    return f"blob-{hashlib.sha256(content).hexdigest()[:12]}"


def _ref_get_response(tip):
    if tip is None:
        return httpx.Response(404, json={"message": "Not Found"})
    return httpx.Response(200, json={"object": {"sha": tip}})


def _blob_post_response(req):
    """The sha is derived from the decoded content, so a corrupted body cannot produce the sha of
    the intended content."""
    body = json.loads(req.content)
    assert body["encoding"] == "base64"
    return httpx.Response(201, json={"sha": _blob_sha(base64.b64decode(body["content"]))})


def _tree_post_response(req, tree_sha, seen_tree):
    body = json.loads(req.content)
    assert "base_tree" not in body
    assert all(i["mode"] == "100644" and i["type"] == "blob" for i in body["tree"])
    if seen_tree is not None:
        for item in body["tree"]:
            seen_tree[item["path"]] = item["sha"]
    return httpx.Response(201, json={"sha": tree_sha})


def _ref_patch_response(req, commit_sha):
    body = json.loads(req.content)
    assert body["sha"] == commit_sha and body["force"] is True
    return httpx.Response(200, json={})


def _ref_post_response(req, commit_sha):
    body = json.loads(req.content)
    assert body == {"ref": "refs/heads/main", "sha": commit_sha}
    return httpx.Response(201, json={})


def _git_data_handler(
    *, tip=None, tip_tree=None, tree_sha="tree-new", commit_sha="commit-new", seen_tree=None
):
    """A git-data backend for ``org/r``: N blobs, one tree built with no ``base_tree``, one commit,
    one ref write. ``tip``/``tip_tree`` describe the branch's existing state (``None``: the branch
    does not exist, the 404 the adapter reads as "new branch"). ``seen_tree``, when a dict,
    records ``{path: blob_sha}`` from the tree POST so a test can pin which path carried which
    content's sha."""
    routes = {
        ("GET", "/repos/acme/r/git/ref/heads/main"): lambda req: _ref_get_response(tip),
        ("GET", f"/repos/acme/r/git/commits/{tip}"): lambda req: httpx.Response(
            200, json={"sha": tip, "tree": {"sha": tip_tree}}
        ),
        ("POST", "/repos/acme/r/git/blobs"): _blob_post_response,
        ("POST", "/repos/acme/r/git/trees"): lambda req: _tree_post_response(req, tree_sha, seen_tree),
        ("POST", "/repos/acme/r/git/commits"): lambda req: httpx.Response(201, json={"sha": commit_sha}),
        ("PATCH", "/repos/acme/r/git/refs/heads/main"): lambda req: _ref_patch_response(req, commit_sha),
        ("POST", "/repos/acme/r/git/refs"): lambda req: _ref_post_response(req, commit_sha),
    }

    def handler(req):
        route = routes.get((req.method, req.url.path))
        if route is None:
            return httpx.Response(404, json={"message": f"unexpected {req.method} {req.url.path}"})
        return route(req)

    return handler


def test_commit_files_issues_exactly_one_commit_for_three_files():
    """The one-commit claim, ON THE WIRE — where a second commit is countable.

    Three blobs, ONE tree, ONE commit, ONE ref write. No in-memory fake can falsify this: the
    regression it guards is an extra ``POST /git/commits``, which is a request, not a state."""
    svc, calls = _svc(_git_data_handler(tip="tip-sha", tip_tree="tree-old"))
    sha = svc.commit_files(
        "acme",
        "r",
        _TEMPLATE,
        branch="main",
        message="chore: initialize from template [skip ci]",
        token=_TOKEN,
        base_url=_BASE,
    )

    assert sha == "commit-new"
    assert calls.count(("POST", "/repos/acme/r/git/blobs")) == 3
    assert calls.count(("POST", "/repos/acme/r/git/trees")) == 1
    assert calls.count(("POST", "/repos/acme/r/git/commits")) == 1
    assert calls.count(("PATCH", "/repos/acme/r/git/refs/heads/main")) == 1
    assert calls[-1] == ("PATCH", "/repos/acme/r/git/refs/heads/main")


def test_commit_files_puts_every_path_in_the_tree_bound_to_its_own_content():
    """PATHS AND CONTENT, pinned — the axis the length-keyed fake made vacuous.

    Two claims the request COUNTS above cannot make:

    (a) the tree carries exactly the pushed paths, unmangled and unprefixed; and
    (b) each path is bound to the sha of *its own* content, so blob shas cannot be rotated onto
        the wrong paths and a blob body cannot be replaced with garbage.

    (b) only bites because the handler's sha is CONTENT-derived. Note ``src/main.py`` and
    ``.github/workflows/build.yml`` are both 12 bytes with different content: under the previous
    length-keyed sha they collided onto one value, which is exactly why a rotation was invisible.
    Their shas must differ."""
    seen: dict[str, str] = {}
    svc, _ = _svc(_git_data_handler(tip="tip-sha", tip_tree="tree-old", seen_tree=seen))
    svc.commit_files("acme", "r", _TEMPLATE, branch="main", message="m", token=_TOKEN, base_url=_BASE)

    assert seen == {path: _blob_sha(content) for path, content in _TEMPLATE.items()}
    assert len(_TEMPLATE["src/main.py"]) == len(_TEMPLATE[".github/workflows/build.yml"])
    assert seen["src/main.py"] != seen[".github/workflows/build.yml"], (
        "equal-length different-content files must not share a sha — a length-keyed fake is "
        "what made the path/content assertions above unable to fail"
    )


def test_commit_files_sends_each_file_content_verbatim():
    """The BYTES on the wire, decoded and compared — so a corrupted or substituted blob body is
    caught at the source rather than inferred from a sha the handler itself minted."""
    posted: list[bytes] = []

    def handler(req):
        if req.method == "POST" and req.url.path == "/repos/acme/r/git/blobs":
            posted.append(base64.b64decode(json.loads(req.content)["content"]))
        return _git_data_handler(tip="tip-sha", tip_tree="tree-old")(req)

    svc, _ = _svc(handler)
    svc.commit_files("acme", "r", _TEMPLATE, branch="main", message="m", token=_TOKEN, base_url=_BASE)

    assert posted == list(_TEMPLATE.values())


@pytest.mark.parametrize("endpoint", ["blobs", "trees", "commits"])
def test_commit_files_surfaces_a_shaless_2xx_as_a_repo_error(endpoint):
    """A 2xx body with no ``sha`` must raise ``GitHubRepoError``, NEVER a raw ``KeyError``.

    ``_require_ok`` cannot catch this: the status is 200 and it is the BODY that is unusable. A
    ``KeyError`` escaping here would leave this file's error contract — every caller catches
    ``GitHubRepoError``, so the materialize step wrapper that should have recorded a failed step
    would instead meet an exception type it does not handle. Checked on all three git-data writes
    because one guarded call site and two unguarded ones is the same bug with better odds."""

    def handler(req):
        if req.method == "POST" and req.url.path == f"/repos/acme/r/git/{endpoint}":
            return httpx.Response(201, json={"url": "https://gh.example/no-sha-here"})
        return _git_data_handler(tip="tip-sha", tip_tree="tree-old")(req)

    svc, _ = _svc(handler)
    with pytest.raises(GitHubRepoError) as ei:
        svc.commit_files("acme", "r", _TEMPLATE, branch="main", message="m", token=_TOKEN, base_url=_BASE)
    assert "sha" in str(ei.value)
    assert _TOKEN not in str(ei.value)


def test_commit_files_carries_the_message_and_parents_the_existing_tip():
    """The message crosses UNCHANGED — it is what carries ``[skip ci]``, so a rewritten one is a
    wasted build. The commit is parented on the branch's tip, so history is extended rather
    than replaced."""
    seen = {}

    def handler(req):
        if req.method == "POST" and req.url.path == "/repos/acme/r/git/commits":
            seen.update(json.loads(req.content))
        return _git_data_handler(tip="tip-sha", tip_tree="tree-old")(req)

    svc, _ = _svc(handler)
    svc.commit_files(
        "acme",
        "r",
        _TEMPLATE,
        branch="main",
        message="chore: initialize from template [skip ci]",
        token=_TOKEN,
        base_url=_BASE,
    )

    assert seen["message"] == "chore: initialize from template [skip ci]"
    assert seen["parents"] == ["tip-sha"]
    assert seen["tree"] == "tree-new"


def test_commit_files_writes_nothing_when_the_tree_sha_already_matches():
    """IDEMPOTENT BY CONTENT — the mechanism, not a promise.

    Git objects are content-addressed, so re-posting identical files yields the SAME tree sha.
    When that sha is already the tip's tree the adapter writes NO commit and does NOT move the
    ref, and the existing tip sha is returned. No ref write means no push event, hence no
    duplicate build — which is the half that made the earlier retry unsafe."""
    svc, calls = _svc(_git_data_handler(tip="tip-sha", tip_tree="tree-new", tree_sha="tree-new"))
    sha = svc.commit_files("acme", "r", _TEMPLATE, branch="main", message="m", token=_TOKEN, base_url=_BASE)

    assert sha == "tip-sha", "the existing tip is the answer, not a fresh commit"
    assert ("POST", "/repos/acme/r/git/commits") not in calls
    assert ("PATCH", "/repos/acme/r/git/refs/heads/main") not in calls


def test_commit_files_creates_the_branch_when_it_does_not_exist():
    """A 404 on the ref is "the branch is new", and the adapter CREATES it — a root commit
    (``parents: []``) plus ``POST /git/refs``.

    That keeps trunk naming the project's business rather than requiring the trunk to be
    whatever ``auto_init`` happened to name."""
    seen = {}

    def handler(req):
        if req.method == "POST" and req.url.path == "/repos/acme/r/git/commits":
            seen.update(json.loads(req.content))
        return _git_data_handler(tip=None)(req)

    svc, calls = _svc(handler)
    sha = svc.commit_files("acme", "r", _TEMPLATE, branch="main", message="m", token=_TOKEN, base_url=_BASE)

    assert sha == "commit-new"
    assert seen["parents"] == []
    assert ("POST", "/repos/acme/r/git/refs") in calls
    assert ("PATCH", "/repos/acme/r/git/refs/heads/main") not in calls


def test_commit_files_does_not_mistake_an_unreadable_ref_for_a_missing_branch():
    """A 500 on the ref read is NOT evidence the branch is absent.

    Conflating them would force a ROOT commit over a branch that already has history — the
    adapter would answer "created" while having discarded the repository's past. Only 404 means
    new; everything else raises."""

    def handler(req):
        if req.method == "GET" and req.url.path == "/repos/acme/r/git/ref/heads/main":
            return httpx.Response(500, json={"message": "Server Error"})
        return httpx.Response(404)

    svc, calls = _svc(handler)
    with pytest.raises(GitHubRepoError) as ei:
        svc.commit_files("acme", "r", _TEMPLATE, branch="main", message="m", token=_TOKEN, base_url=_BASE)
    assert "500" in str(ei.value)
    assert _TOKEN not in str(ei.value)
    assert ("POST", "/repos/acme/r/git/commits") not in calls


def test_commit_files_refuses_an_empty_file_set_before_any_request():
    """An empty mapping would build an EMPTY tree — deleting the branch's whole contents.
    Refused BEFORE any HTTP, so a caller with nothing to push cannot destroy a repo by
    accident."""

    def handler(req):
        raise AssertionError("must refuse before issuing a request")

    svc, calls = _svc(handler)
    with pytest.raises(GitHubRepoError) as ei:
        svc.commit_files("acme", "r", {}, branch="main", message="m", token=_TOKEN, base_url=_BASE)
    assert "empty" in str(ei.value)
    assert calls == []


def test_commit_files_transport_error_is_safe_and_tokenless():
    def handler(req):
        raise httpx.ReadTimeout("slow")

    svc, _ = _svc(handler)
    with pytest.raises(GitHubRepoError) as ei:
        svc.commit_files("acme", "r", _TEMPLATE, branch="main", message="m", token=_TOKEN, base_url=_BASE)
    text = str(ei.value)
    assert _TOKEN not in text and "slow" not in text
    assert "ReadTimeout" in text


def test_set_ci_vars_with_no_scope_writes_the_repository_wide_set():
    """``scope=None`` ⇒ the repo-level Actions ``vars``. This is the ONLY scope the materialize
    path uses: a named one must already exist, and creating it would be a second write."""
    written = {}

    def handler(req):
        if req.method == "POST" and req.url.path == "/repos/acme/r/actions/variables":
            body = json.loads(req.content)
            written[body["name"]] = body["value"]
            return httpx.Response(201, json={})
        return httpx.Response(404)

    svc, _ = _svc(handler)
    svc.set_ci_vars(
        "acme", "r", {"AWS_REGION": "us-east-1", "AGENT_ID": "a1"}, scope=None, token=_TOKEN, base_url=_BASE
    )

    assert written == {"AWS_REGION": "us-east-1", "AGENT_ID": "a1"}


def test_set_ci_vars_with_a_scope_writes_under_that_scope():
    """A named scope is the PROVIDER's grouping (a GitHub Environment), not a stage. AGP names
    no stage here, so a tenant with three stages needs no change to this method."""
    paths = []

    def handler(req):
        if req.method == "POST" and "variables" in req.url.path:
            paths.append(req.url.path)
            return httpx.Response(201, json={})
        return httpx.Response(404)

    svc, _ = _svc(handler)
    svc.set_ci_vars("acme", "r", {"X": "1"}, scope="staging-eu", token=_TOKEN, base_url=_BASE)

    assert paths == ["/repos/acme/r/environments/staging-eu/variables"]


def test_set_ci_vars_patches_on_409_so_a_rerun_converges():
    """POST creates; a 409 means it is already there, so PATCH the named variable. Without this
    a retried materialize fails on every variable it already wrote."""

    def handler(req):
        p, m = req.url.path, req.method
        if m == "POST" and p == "/repos/acme/r/actions/variables":
            return httpx.Response(409, json={"message": "already exists"})
        if m == "PATCH" and p == "/repos/acme/r/actions/variables/AGENT_ID":
            assert json.loads(req.content) == {"name": "AGENT_ID", "value": "a1"}
            return httpx.Response(204)
        return httpx.Response(404)

    svc, calls = _svc(handler)
    svc.set_ci_vars("acme", "r", {"AGENT_ID": "a1"}, scope=None, token=_TOKEN, base_url=_BASE)

    assert ("PATCH", "/repos/acme/r/actions/variables/AGENT_ID") in calls


def test_set_ci_vars_skips_empty_values_entirely():
    """An unset optional variable stays ABSENT rather than arriving as "". A build reading one
    cannot tell an empty string from a deliberate blank; ``LANGFUSE_SECRET_NAME`` once reached
    the container as exactly that and tracing could not authenticate."""
    names = []

    def handler(req):
        if req.method == "POST":
            names.append(json.loads(req.content)["name"])
            return httpx.Response(201, json={})
        return httpx.Response(404)

    svc, _ = _svc(handler)
    svc.set_ci_vars(
        "acme", "r", {"KEEP": "v", "BLANK": "", "MISSING": None}, scope=None, token=_TOKEN, base_url=_BASE
    )

    assert names == ["KEEP"]


def test_set_ci_vars_raises_a_safe_message_on_failure():
    def handler(req):
        return httpx.Response(403, json={"message": "Resource not accessible by integration"})

    svc, _ = _svc(handler)
    with pytest.raises(GitHubRepoError) as ei:
        svc.set_ci_vars("acme", "r", {"X": "1"}, scope=None, token=_TOKEN, base_url=_BASE)
    text = str(ei.value)
    assert "403" in text and "acme/r" in text
    assert _TOKEN not in text


def _tree_entry(path, content, mode="100644", type_="blob"):
    return {"path": path, "mode": mode, "type": type_, "sha": _blob_sha(content), "size": len(content)}


def _tree_handler(
    *, files=None, truncated=False, extra_entries=(), ref="head-sha", blob_overrides=None, blobs_seen=None
):
    """A git-data READ backend for ``acme/r`` at ``ref``.

    ``files`` is the repository's real content. The blob endpoint is keyed by the CONTENT's own
    sha, so the handler cannot be asked for "the blob at path X" — only "the blob whose content
    hashes to S". A reader that bound the wrong sha to a path therefore gets the wrong bytes,
    which is what makes the path/content assertions able to fail.

    ``extra_entries`` injects raw tree entries (a symlink, a submodule, a subtree) that carry no
    matching blob. ``blob_overrides`` replaces a specific sha's RESPONSE (to model a 2xx whose
    body is unusable). ``blobs_seen`` records the order blob shas were requested in."""
    files = _TEMPLATE if files is None else files
    by_sha = {_blob_sha(c): c for c in files.values()}
    entries = [_tree_entry(p, c) for p, c in files.items()] + list(extra_entries)

    def handler(req):
        p, m = req.url.path, req.method
        if m == "GET" and p == f"/repos/acme/r/git/trees/{ref}":
            assert req.url.params.get("recursive") == "1", (
                "a non-recursive listing reports only the top level — a template's src/ would "
                "silently arrive as a directory entry with no files"
            )
            return httpx.Response(200, json={"sha": ref, "tree": entries, "truncated": truncated})
        if m == "GET" and p.startswith("/repos/acme/r/git/blobs/"):
            sha = p.rsplit("/", 1)[-1]
            if blobs_seen is not None:
                blobs_seen.append(sha)
            if blob_overrides and sha in blob_overrides:
                return blob_overrides[sha]
            if sha not in by_sha:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(
                200,
                json={
                    "sha": sha,
                    "encoding": "base64",
                    "content": base64.b64encode(by_sha[sha]).decode("ascii"),
                },
            )
        return httpx.Response(404, json={"message": f"unexpected {m} {p}"})

    return handler


def test_read_tree_returns_every_path_bound_to_its_own_bytes():
    """The INVERSE of ``commit_files`` on the wire: ``dict[path, bytes]``, byte for byte.

    Whole-mapping equality, so a dropped path, an added one, a mangled key or a decoded ``str``
    all redden this. The bytes are real: the fake serves blobs from a CONTENT-keyed map, so
    returning ``src/main.py``'s bytes under ``requirements.txt`` cannot pass — note again that
    ``src/main.py`` and ``.github/workflows/build.yml`` are equal-length and different-content,
    the exact pair a length-keyed fake collapsed."""
    svc, calls = _svc(_tree_handler())
    got = svc.read_tree("acme", "r", ref="head-sha", token=_TOKEN, base_url=_BASE)

    assert got == _TEMPLATE
    assert all(isinstance(v, bytes) for v in got.values()), "bytes, never str (tenet 5)"
    assert calls.count(("GET", "/repos/acme/r/git/trees/head-sha")) == 1
    assert sum(1 for _m, p in calls if p.startswith("/repos/acme/r/git/blobs/")) == 3


def test_read_tree_reads_at_the_REF_it_was_given_and_no_other():
    """``ref`` reaches the URL — the pin that makes a mid-read push harmless.

    A reader that ignored ``ref`` (or resolved HEAD itself) would answer whatever the branch is
    NOW, so a template author's push landing between the head-sha probe and the tree read would
    have half of each template committed into a customer's repo.

    THE MUTATION THIS REDDENS: both refs list the SAME path with DIFFERENT content, and the blob
    endpoint serves both contents — so an implementation that read the right listing but the
    wrong blob, or the wrong listing entirely, gets the other ref's bytes rather than a 404 it
    could be excused for. The failure is a content mismatch, which is the only kind that proves
    the ref reached the read."""
    old_bytes, new_bytes = b"# the old template\n", b"# the new template\n"
    by_sha = {_blob_sha(old_bytes): old_bytes, _blob_sha(new_bytes): new_bytes}

    def handler(req):
        p, m = req.url.path, req.method
        if m == "GET" and p in ("/repos/acme/r/git/trees/old-sha", "/repos/acme/r/git/trees/new-sha"):
            content = old_bytes if p.endswith("old-sha") else new_bytes
            return httpx.Response(
                200,
                json={
                    "tree": [_tree_entry("src/main.py", content)],
                    "truncated": False,
                },
            )
        if m == "GET" and p.startswith("/repos/acme/r/git/blobs/"):
            sha = p.rsplit("/", 1)[-1]
            return httpx.Response(
                200,
                json={
                    "sha": sha,
                    "encoding": "base64",
                    "content": base64.b64encode(by_sha[sha]).decode("ascii"),
                },
            )
        return httpx.Response(404)

    svc, calls = _svc(handler)
    got = svc.read_tree("acme", "r", ref="old-sha", token=_TOKEN, base_url=_BASE)

    assert got == {"src/main.py": old_bytes}
    assert ("GET", "/repos/acme/r/git/trees/old-sha") in calls
    assert ("GET", "/repos/acme/r/git/trees/new-sha") not in calls


def test_read_tree_refuses_a_blank_ref_before_issuing_any_request():
    """A blank ``ref`` FAILS CLOSED rather than defaulting to a branch.

    Substituting HEAD for a missing ref is the mixed-tree defect with its guard removed, and it
    would turn a caller's bug (a record with no head sha) into a silently different answer. No
    request is issued, so the refusal cannot be mistaken for a provider 404."""

    def handler(req):
        raise AssertionError(f"must refuse a blank ref before any request, sent {req.url}")

    svc, calls = _svc(handler)
    with pytest.raises(GitHubRepoError) as ei:
        svc.read_tree("acme", "r", ref="", token=_TOKEN, base_url=_BASE)
    assert "ref" in str(ei.value)
    assert calls == []


@pytest.mark.parametrize(
    "evil_ref",
    [
        "../../../orgs/other-org/repos",
        "a/../../b",
        "/refs/heads/main",
        "%2e%2e/%2e%2e/orgs/other-org",
        "..%2f..%2forgs/other-org",
        "a/..%2f%2e%2e/b",
    ],
)
def test_read_tree_refuses_a_ref_that_would_REDIRECT_the_request(evil_ref):
    """A ``ref`` is interpolated into the URL, so it must not be able to steer the request.

    ``httpx`` normalises ``..`` in a path before sending, as a browser does, so a traversing ref
    does not 404: it asks GitHub a different question under AGP's own App token and this method
    would return the answer as a template's file tree. The percent-encoded forms hide the literal
    ``..`` from a naive guard, and ``httpx`` sends the escape through literally, so ``%`` is refused
    outright; it is not legal in a git ref name. ``ref`` is not operator-typed (it arrives from a
    DynamoDB template record or a provider response), so a malformed one is a bug to state rather
    than a value to sanitise, and no request is issued."""

    def handler(req):
        raise AssertionError(f"a traversing ref must be refused before any request: {req.url}")

    svc, calls = _svc(handler)
    with pytest.raises(GitHubRepoError) as ei:
        svc.read_tree("acme", "r", ref=evil_ref, token=_TOKEN, base_url=_BASE)
    assert "ref" in str(ei.value)
    assert calls == [], "a traversing ref must never reach the wire"


@pytest.mark.parametrize(
    "evil_ref",
    [
        "main?per_page=1",
        "main?recursive=0",
        "main#fragment",
    ],
)
def test_read_tree_refuses_a_ref_that_would_inject_a_QUERY_or_a_FRAGMENT(evil_ref):
    """``httpx`` merges a ``?`` parameter into the query beside AGP's own ``recursive=1`` (the
    parameter this method's completeness depends on) and drops a ``#`` fragment, so the read would
    silently succeed against a different ref. Neither character is legal in a git ref name, and no
    request is issued."""

    def handler(req):
        raise AssertionError(f"a query/fragment-injecting ref must never reach the wire: {req.url}")

    svc, calls = _svc(handler)
    with pytest.raises(GitHubRepoError) as ei:
        svc.read_tree("acme", "r", ref=evil_ref, token=_TOKEN, base_url=_BASE)
    assert "ref" in str(ei.value)
    assert calls == []


@pytest.mark.parametrize("ok_ref", ["3e2b70b6", "refs/heads/main", "feature/some-branch", "v1.2.3"])
def test_read_tree_accepts_the_ref_shapes_a_caller_legitimately_passes(ok_ref):
    """The negative of the guard above, so "refuse a traversal" cannot become "refuse a slash".

    A head sha is the contract's expected value, but ``refs/heads/main`` and ``feature/x`` are
    valid git refs containing slashes — and git's own ``check-ref-format`` forbids ``..`` while
    allowing these, so the guard and git agree."""
    svc, calls = _svc(_tree_handler(files={"a.py": b"x"}, ref=ok_ref))
    assert svc.read_tree("acme", "r", ref=ok_ref, token=_TOKEN, base_url=_BASE) == {"a.py": b"x"}
    assert ("GET", f"/repos/acme/r/git/trees/{ok_ref}") in calls


def test_read_tree_RAISES_when_github_says_the_listing_was_truncated():
    """A TRUNCATED listing must raise, not return the part that arrived.

    A partial that looks complete is the failure class this seam exists to end. GitHub caps a
    recursive tree listing and sets ``truncated: true`` when it did — the entries present are
    perfectly valid, which is precisely the danger. Materializing them would commit a template
    silently missing files, and ``commit_files`` builds its tree with no ``base_tree``, so the
    absence would be a DELETION on a re-push rather than an omission."""
    svc, _ = _svc(_tree_handler(truncated=True))
    with pytest.raises(GitHubRepoError) as ei:
        svc.read_tree("acme", "r", ref="head-sha", token=_TOKEN, base_url=_BASE)
    text = str(ei.value)
    assert "truncat" in text.lower()
    assert _TOKEN not in text


@pytest.mark.parametrize(
    "kind,entry",
    [
        ("symlink", {"path": "link.py", "mode": "120000", "type": "blob", "sha": "lnk", "size": 9}),
        ("submodule", {"path": "vendor/lib", "mode": "160000", "type": "commit", "sha": "sub"}),
    ],
)
def test_read_tree_RAISES_on_a_symlink_or_a_submodule(kind, entry):
    """Neither is a blob AGP can carry, and SKIPPING would be the quiet lie.

    A symlink's blob content is the TARGET PATH, so writing it back through ``commit_files``
    (mode 100644, always) would replace a link with a text file containing the text of its
    target — a repo that materializes and then does not run. A submodule is a pointer into a
    repository this seam cannot read at all, so its content simply is not here.

    Raising tells a template author their repo holds something AGP does not carry. Silently
    dropping the entry would materialize a template that is missing part of itself, with a green
    step beside it — the failure mode this epic exists to end.

    THE MESSAGE MUST NAME THE KIND, not merely refuse. Found by mutation: deleting the symlink
    branch left this test GREEN, because the mode whitelist below it refused mode 120000 anyway
    with a generic "not a plain file". The operator-facing difference is the whole value — "your
    repo has a symlink AGP cannot carry" is actionable, "mode '120000'" is a puzzle — so the word
    is asserted and the specific branch is now load-bearing."""
    svc, _ = _svc(_tree_handler(extra_entries=(entry,)))
    with pytest.raises(GitHubRepoError) as ei:
        svc.read_tree("acme", "r", ref="head-sha", token=_TOKEN, base_url=_BASE)
    text = str(ei.value)
    assert entry["path"] in text, "the message must name WHICH entry, so it is actionable"
    assert kind in text.lower(), f"the message must say {kind!r}, not just refuse a mode"
    assert _TOKEN not in text


def test_read_tree_skips_only_directory_entries_and_never_requests_a_blob_for_one():
    """A ``tree`` entry is a DIRECTORY, and it is the one kind that is legitimately not content.

    With ``recursive=1`` GitHub lists the directories alongside their files, and the files' paths
    are already fully qualified — so a directory carries nothing and asking for its blob would
    404. It is skipped, and it is the ONLY thing skipped: this test exists so "raise on symlinks
    and submodules" cannot be implemented as "skip whatever is not mode 100644"."""
    seen: list[str] = []
    dir_entry = {"path": "src", "mode": "040000", "type": "tree", "sha": "dir-sha"}
    svc, _ = _svc(_tree_handler(extra_entries=(dir_entry,), blobs_seen=seen))

    got = svc.read_tree("acme", "r", ref="head-sha", token=_TOKEN, base_url=_BASE)

    assert got == _TEMPLATE
    assert "src" not in got
    assert "dir-sha" not in seen, "a directory has no blob to fetch"


def test_read_tree_refuses_a_mode_it_does_not_recognise_rather_than_carrying_it():
    """The ALLOWLIST, not just the two named refusals. Mode 100644 and 100755 are files AGP can
    carry; everything else — including a mode git does not currently mint — is refused.

    Written because mutation showed the allowlist had no test of its own: the symlink and
    submodule cases were each caught by their named branch, so deleting the allowlist changed
    nothing observable. A denylist of the two kinds known today is the shape that quietly carries
    the third, and this method's entire promise is that a partial or wrong tree fails loudly."""
    weird = {"path": "odd", "mode": "100600", "type": "blob", "sha": _blob_sha(b"x"), "size": 1}
    svc, _ = _svc(_tree_handler(extra_entries=(weird,)))
    with pytest.raises(GitHubRepoError) as ei:
        svc.read_tree("acme", "r", ref="head-sha", token=_TOKEN, base_url=_BASE)
    text = str(ei.value)
    assert "odd" in text and "100600" in text
    assert _TOKEN not in text


def test_read_tree_carries_an_executable_file_through_rather_than_refusing_it():
    """Mode 100755 is a PLAIN FILE that happens to be executable — a template's ``entrypoint.sh``.

    It is content AGP can carry, so it must not be swept up by the symlink/submodule refusal.
    (The bit itself does not survive ``commit_files``, which writes 100644; losing an execute bit
    is a known, bounded cost, and it is strictly better than refusing every template that ships a
    shell script.)"""
    exe = b"#!/bin/sh\nexec python -m app\n"
    files = dict(_TEMPLATE, **{"entrypoint.sh": exe})
    entries = [_tree_entry(p, c) for p, c in _TEMPLATE.items()] + [
        _tree_entry("entrypoint.sh", exe, mode="100755")
    ]

    def handler(req):
        if req.url.path == "/repos/acme/r/git/trees/head-sha":
            return httpx.Response(200, json={"tree": entries, "truncated": False})
        return _tree_handler(files=files)(req)

    svc, _ = _svc(handler)
    assert svc.read_tree("acme", "r", ref="head-sha", token=_TOKEN, base_url=_BASE)["entrypoint.sh"] == exe


def test_read_tree_rejects_a_path_that_would_escape_the_tree_root():
    """Paths are ROOT-RELATIVE. A leading slash or a ``..`` segment must not pass through.

    What comes back here is fed straight into ``commit_files`` — and on the seed path, sibling
    code writes to disk (a traversal was closed there once). A provider that answered
    ``../../etc/x`` would be a surprise worth stating rather than normalising away: normalising
    silently rewrites a template author's layout, and passing it on hands a traversal to
    whichever consumer is least careful.

    THE BAD ENTRY'S BLOB IS FULLY SERVABLE, and that is the point. Found by mutation: with the
    entry's blob absent, deleting the path check still left this test green — the read merely
    404'd on a blob it could not fetch, so the test proved nothing about paths. Both bad paths
    below carry real content the handler will happily serve, so the ONLY thing that can refuse
    them is the path check itself."""
    for bad_path in ("../escape.py", "/etc/passwd"):
        content = b"x"
        entry = _tree_entry(bad_path, content)
        files = dict(_TEMPLATE, **{bad_path: content})
        svc, _ = _svc(_tree_handler(files=files, extra_entries=()))
        assert entry["sha"] == _blob_sha(content)
        with pytest.raises(GitHubRepoError) as ei:
            svc.read_tree("acme", "r", ref="head-sha", token=_TOKEN, base_url=_BASE)
        assert bad_path in str(ei.value), f"{bad_path} must be refused BY NAME"
        assert _TOKEN not in str(ei.value)


def test_read_tree_surfaces_a_2xx_blob_with_no_content_as_a_repo_error():
    """A 200 whose body has no ``content`` raises ``GitHubRepoError``, NEVER a ``KeyError``.

    The same defect ``_sha_of`` closed on the write path, on the read path: ``_require_ok``
    cannot see it because the STATUS is fine and it is the BODY that is unusable, and a
    ``KeyError`` escapes this module's error contract — every caller catches
    ``GitHubRepoError``, so the materialize step wrapper meets a type it does not handle. The
    alternative failure is worse: a ``None`` content would commit an EMPTY file over a real
    one."""
    sha = _blob_sha(_TEMPLATE["src/main.py"])
    svc, _ = _svc(
        _tree_handler(
            blob_overrides={
                sha: httpx.Response(200, json={"sha": sha, "encoding": "base64"}),
            }
        )
    )
    with pytest.raises(GitHubRepoError) as ei:
        svc.read_tree("acme", "r", ref="head-sha", token=_TOKEN, base_url=_BASE)
    text = str(ei.value)
    assert "src/main.py" in text
    assert _TOKEN not in text


def test_read_tree_refuses_a_blob_github_would_not_encode():
    """GitHub answers ``encoding: "none"`` with an EMPTY content for a blob over its inline size
    limit. Decoding that as base64 yields ``b""`` — a real file silently becoming an empty one,
    which then commits as a truncation rather than a failure. The encoding is therefore checked
    rather than assumed."""
    sha = _blob_sha(_TEMPLATE["requirements.txt"])
    svc, _ = _svc(
        _tree_handler(
            blob_overrides={
                sha: httpx.Response(200, json={"sha": sha, "encoding": "none", "content": ""}),
            }
        )
    )
    with pytest.raises(GitHubRepoError) as ei:
        svc.read_tree("acme", "r", ref="head-sha", token=_TOKEN, base_url=_BASE)
    text = str(ei.value)
    assert "requirements.txt" in text and "encoding" in text.lower()


@pytest.mark.parametrize("body", [[], {"truncated": False}, {"tree": "not-a-list"}, "a string"])
def test_read_tree_surfaces_a_tree_listing_of_the_wrong_SHAPE_as_a_repo_error(body):
    """A 2xx tree listing that is not ``{"tree": [...]}`` must raise ``GitHubRepoError``.

    The failure this closes is not "a wrong answer" but a WRONG EXCEPTION TYPE: without the shape
    check, a list body makes ``body.get("truncated")`` an ``AttributeError`` and a missing
    ``tree`` key a ``KeyError``, and both ESCAPE this module's contract — every caller catches
    ``GitHubRepoError``, so the materialize step wrapper that should record a failed step meets a
    type it does not handle. Same discipline as ``_sha_of`` on the write path. Written because
    mutation showed the check had no test: the parametrisation covers each shape independently, so
    one guarded branch and three unguarded ones cannot pass."""

    def handler(req):
        if req.url.path == "/repos/acme/r/git/trees/head-sha":
            return httpx.Response(200, json=body)
        return httpx.Response(404)

    svc, _ = _svc(handler)
    with pytest.raises(GitHubRepoError) as ei:
        svc.read_tree("acme", "r", ref="head-sha", token=_TOKEN, base_url=_BASE)
    assert "unusable" in str(ei.value)
    assert _TOKEN not in str(ei.value)


def test_read_tree_raises_a_safe_message_when_the_ref_is_unknown():
    """A 404 on the tree is a FAILED READ, not an empty repository.

    Returning ``{}`` would be catastrophic downstream: ``commit_files`` refuses an empty mapping,
    but the reconcile surface comparing trees would report a repo as having no content. And the
    caller asked for a specific sha — one that does not resolve means the record and the provider
    disagree, which is a state to state, not to smooth over."""

    def handler(req):
        return httpx.Response(404, json={"message": "Not Found"})

    svc, _ = _svc(handler)
    with pytest.raises(GitHubRepoError) as ei:
        svc.read_tree("acme", "r", ref="gone-sha", token=_TOKEN, base_url=_BASE)
    text = str(ei.value)
    assert "404" in text and "acme/r" in text
    assert _TOKEN not in text


def test_read_tree_transport_error_is_safe_and_tokenless():
    def handler(req):
        raise httpx.ReadTimeout("slow")

    svc, _ = _svc(handler)
    with pytest.raises(GitHubRepoError) as ei:
        svc.read_tree("acme", "r", ref="head-sha", token=_TOKEN, base_url=_BASE)
    text = str(ei.value)
    assert _TOKEN not in text and "slow" not in text
    assert "ReadTimeout" in text


@pytest.mark.parametrize(
    "entry",
    [
        "not-a-dict",
        None,
        {"mode": "100644", "type": "blob", "sha": "s"},
        {"path": "", "mode": "100644", "type": "blob", "sha": "s"},
        {"path": "x.py", "mode": "100644", "type": "blob"},
        {"path": "x.py", "mode": "100644", "type": "blob", "sha": ""},
        {"path": "x.py", "mode": "100644", "type": "blob", "sha": None},
    ],
    ids=["not_a_dict", "none", "no_path", "blank_path", "no_sha", "blank_sha", "null_sha"],
)
def test_read_tree_surfaces_a_malformed_tree_ENTRY_through_the_error_contract(entry):
    """A per-ENTRY surprise, not a per-BODY one. An absent ``path`` would otherwise reach the
    root-relative check as ``None`` (``AttributeError`` on ``.startswith``) and an absent ``sha``
    would build a ``/git/blobs/None`` URL — both leaving this module's error contract or asking
    the provider a nonsense question.

    NO BLOB IS REQUESTED for an entry the listing already showed to be unusable. Mutation found
    this: dropping the sha check alone still raised ``GitHubRepoError``, because
    ``/git/blobs/None`` 404s and ``_require_ok`` reports it — the same exception type by accident,
    after spending a round trip to ask GitHub a question with a literal ``None`` in the URL. The
    request count is what makes the guard's own behaviour observable."""
    seen: list[str] = []
    svc, _ = _svc(_tree_handler(extra_entries=(entry,), blobs_seen=seen))
    with pytest.raises(GitHubRepoError) as ei:
        svc.read_tree("acme", "r", ref="head-sha", token=_TOKEN, base_url=_BASE)
    assert _TOKEN not in str(ei.value)
    assert "None" not in seen and "" not in seen, (
        "a malformed entry must be refused from the LISTING, not by asking GitHub for a blob "
        f"whose sha the listing never named — requested: {seen}"
    )


@pytest.mark.parametrize("blob_body", ["not-a-dict", None, ["a", "list"], 7])
def test_read_tree_surfaces_a_malformed_BLOB_body_through_the_error_contract(blob_body):
    """The blob read's own shape guard. A non-dict body makes ``.get("encoding")`` an
    ``AttributeError``, which is the leak — not the malformed body itself."""
    sha = _blob_sha(_TEMPLATE["src/main.py"])
    svc, _ = _svc(_tree_handler(blob_overrides={sha: httpx.Response(200, json=blob_body)}))
    with pytest.raises(GitHubRepoError) as ei:
        svc.read_tree("acme", "r", ref="head-sha", token=_TOKEN, base_url=_BASE)
    assert _TOKEN not in str(ei.value)


@pytest.mark.parametrize("repo_body", ["not-a-dict", None, ["a", "list"]])
def test_read_repo_surfaces_a_malformed_repository_body_through_the_error_contract(repo_body):
    """A 200 on ``/repos/{org}/{repo}`` whose body is not an object. Shared by ``repo_exists``
    through ``_read_repo_body``, so this pins BOTH readers at once — which is the reason that
    helper is one authority rather than two copies."""

    def handler(req):
        if req.url.path == "/repos/acme/r":
            return httpx.Response(200, json=repo_body)
        return httpx.Response(404)

    svc, _ = _svc(handler)
    with pytest.raises(GitHubRepoError) as ei:
        svc.read_repo("acme", "r", token=_TOKEN, base_url=_BASE)
    assert _TOKEN not in str(ei.value)
    svc2, _ = _svc(handler)
    with pytest.raises(GitHubRepoError):
        svc2.repo_exists("acme", "r", _TOKEN, base_url=_BASE)


@pytest.mark.parametrize(
    "ref_body", [{}, {"object": {}}, {"object": None}, {"object": {"sha": ""}}, "not-a-dict"]
)
def test_read_repo_raises_when_the_branch_ref_names_no_commit(ref_body):
    """A 200 ref read that carries no sha is NOT an empty repository.

    The distinction is load-bearing: a 404 on the ref means "no commit yet" and answers a view
    with an empty ``head_sha``, while a 200 that simply lacks the sha is a provider surprise. If
    this returned an empty ``head_sha`` too, a caller would pass ``ref=""`` into ``read_tree`` —
    which now fails closed, but only because that guard exists. Say so here instead."""

    def handler(req):
        if req.url.path == "/repos/acme/r":
            return httpx.Response(200, json={"default_branch": "main"})
        if req.url.path == "/repos/acme/r/git/ref/heads/main":
            return httpx.Response(200, json=ref_body)
        return httpx.Response(404)

    svc, _ = _svc(handler)
    with pytest.raises(GitHubRepoError) as ei:
        svc.read_repo("acme", "r", token=_TOKEN, base_url=_BASE)
    assert "no commit" in str(ei.value)
    assert _TOKEN not in str(ei.value)


def _repo_handler(*, default_branch="main", tip="head-sha", repo_status=200, ref_status=None):
    """A ``read_repo`` backend: the repo GET names the default branch, the ref GET carries its
    tip. TWO calls because GitHub's repository object holds no head sha and no endpoint carries
    both — adapter-internal plumbing behind a one-call seam."""

    def handler(req):
        p, m = req.url.path, req.method
        if m == "GET" and p == "/repos/acme/r":
            if repo_status != 200:
                return httpx.Response(repo_status, json={"message": "nope"})
            return httpx.Response(200, json={"name": "r", "default_branch": default_branch})
        if m == "GET" and p == f"/repos/acme/r/git/ref/heads/{default_branch}":
            if ref_status is not None:
                return httpx.Response(ref_status, json={"message": "nope"})
            return httpx.Response(200, json={"object": {"sha": tip}})
        return httpx.Response(404, json={"message": f"unexpected {m} {p}"})

    return handler


def test_read_repo_reports_the_default_branch_and_its_tip():
    """The reconcile probe's whole job, and the head sha it yields is what ``read_tree`` is then
    called with — resolve once, read at that sha.

    The default branch is READ, never assumed to be "main": a customer's template repo may use
    any trunk, and guessing would read the wrong tree or none at all."""
    svc, calls = _svc(_repo_handler(default_branch="trunk", tip="abc1234"))
    view = svc.read_repo("acme", "r", token=_TOKEN, base_url=_BASE)

    assert isinstance(view, RepoView)
    assert view.default_branch == "trunk"
    assert view.head_sha == "abc1234"
    assert ("GET", "/repos/acme/r") in calls
    assert ("GET", "/repos/acme/r/git/ref/heads/trunk") in calls


def test_read_repo_answers_none_ONLY_for_a_404():
    """``None`` IS NOT-FOUND. This is what makes a registry row read ``registered_missing`` and
    offers "re-create from seed", so nothing else may produce it."""

    def handler(req):
        return httpx.Response(404, json={"message": "Not Found"})

    svc, _ = _svc(handler)
    assert svc.read_repo("acme", "gone", token=_TOKEN, base_url=_BASE) is None


@pytest.mark.parametrize("status", [401, 403, 500, 502])
def test_read_repo_RAISES_rather_than_reporting_missing_on_auth_or_transport_failure(status):
    """AN OUTAGE IS NOT AN ABSENCE — the most consequential pin in this task.

    Folding a 401/403/5xx into ``None`` would tell an operator every template repo is gone and
    offer to re-create them from the on-disk seed, i.e. it would propose overwriting the
    customer's iterated templates with starter bytes because a token expired. "AGP could not
    look" must be a raise. 403 is included deliberately: it is what a rate limit and a
    missing-permission both answer, and both are conditions AGP is wrong about the world."""
    svc, _ = _svc(_repo_handler(repo_status=status))
    with pytest.raises(GitHubRepoError) as ei:
        svc.read_repo("acme", "r", token=_TOKEN, base_url=_BASE)
    text = str(ei.value)
    assert str(status) in text and "acme/r" in text
    assert _TOKEN not in text


def test_read_repo_reports_an_empty_repository_as_present_with_no_tip():
    """A repo that EXISTS with no commit yet: present, ``head_sha`` empty.

    Not ``None`` — reconcile would call it missing and offer to create what is already there
    (and the create would 422). Not a raise — nothing failed. A freshly created repo is exactly
    this state, and ``create_repo`` produces one on every materialize, so the seam has to
    be able to describe it."""
    svc, _ = _svc(_repo_handler(ref_status=404))
    view = svc.read_repo("acme", "r", token=_TOKEN, base_url=_BASE)

    assert view is not None
    assert view.default_branch == "main"
    assert view.head_sha == ""


def test_read_repo_raises_on_a_2xx_that_names_no_default_branch():
    """A 200 without ``default_branch`` is unusable, and the failure must be THIS module's type.

    A ``KeyError`` (or a ``None`` branch folded into the next URL, producing
    ``/git/ref/heads/None``) escapes the error contract every caller catches. Same discipline as
    ``_sha_of`` on the write path."""

    def handler(req):
        if req.url.path == "/repos/acme/r":
            return httpx.Response(200, json={"name": "r"})
        return httpx.Response(404)

    svc, _ = _svc(handler)
    with pytest.raises(GitHubRepoError) as ei:
        svc.read_repo("acme", "r", token=_TOKEN, base_url=_BASE)
    assert "default branch" in str(ei.value).lower()
    assert _TOKEN not in str(ei.value)


def test_read_repo_transport_error_is_safe_and_tokenless():
    def handler(req):
        raise httpx.ConnectError("boom")

    svc, _ = _svc(handler)
    with pytest.raises(GitHubRepoError) as ei:
        svc.read_repo("acme", "r", token=_TOKEN, base_url=_BASE)
    text = str(ei.value)
    assert _TOKEN not in text and "boom" not in text


def _pages_handler(pages, *, status=200):
    """Serve ``pages`` (a list of lists of repo objects) from ``GET /orgs/acme/repos?page=N``.
    A page shorter than the requested ``per_page`` is the last one — GitHub's own signal."""

    def handler(req):
        if req.method == "GET" and req.url.path == "/orgs/acme/repos":
            if status != 200:
                return httpx.Response(status, json={"message": "nope"})
            page = int(req.url.params.get("page", "1"))
            body = pages[page - 1] if 1 <= page <= len(pages) else []
            return httpx.Response(200, json=body)
        return httpx.Response(404, json={"message": "unexpected"})

    return handler


def test_list_repos_returns_names_only():
    """NAMES, not the provider's repository bodies. The caller subtracts what AGP already
    accounts for and offers the rest for adoption — a name is all that needs, and a body crossing
    here would be the pass-through every projection in this seam avoids."""
    svc, _ = _svc(
        _pages_handler(
            [
                [
                    {"name": "strands-agentcore", "private": True, "html_url": "u", "id": 1},
                    {"name": "agp-runtime-infra", "private": True, "html_url": "u2", "id": 2},
                ]
            ]
        )
    )
    got = svc.list_repos("acme", token=_TOKEN, base_url=_BASE)

    assert got == ["strands-agentcore", "agp-runtime-infra"]
    assert all(isinstance(n, str) for n in got)


def test_list_repos_follows_every_page():
    """PAGINATION IS ADAPTER-INTERNAL, and it must actually happen.

    An org with more repos than one page is the ordinary case, and a reader that stopped at page
    one would show an operator a partial org — repos already adopted looking adoptable, repos
    they were looking for absent. The full-page/short-page boundary is what ends the walk."""
    per_page_full = [{"name": f"r{i}"} for i in range(100)]
    svc, calls = _svc(_pages_handler([per_page_full, [{"name": "last"}]]))

    got = svc.list_repos("acme", token=_TOKEN, base_url=_BASE)

    assert len(got) == 101
    assert got[-1] == "last"
    assert sum(1 for _m, p in calls if p == "/orgs/acme/repos") == 2


def test_list_repos_stops_at_the_last_page_without_an_extra_request():
    """A short page ends the walk. A reader that kept going until an EMPTY page spends an extra
    request on every single call — small alone, and Bitbucket's 1,000 req/h is the binding budget
    this surface is costed against."""
    svc, calls = _svc(_pages_handler([[{"name": "only"}]]))
    assert svc.list_repos("acme", token=_TOKEN, base_url=_BASE) == ["only"]
    assert sum(1 for _m, p in calls if p == "/orgs/acme/repos") == 1


def test_list_repos_RAISES_rather_than_returning_a_silently_capped_list():
    """The adapter's own page cap RAISES when reached — it does not return what it had.

    A cap that returns quietly is the same silent partial ``read_tree`` refuses, one layer over:
    the operator would see a list that looks like their whole org and adopt against it. Modelled
    by an endpoint that never runs out of full pages, which is also what a pagination bug looks
    like from here."""
    endless = [{"name": f"r{i}"} for i in range(100)]

    def handler(req):
        if req.method == "GET" and req.url.path == "/orgs/acme/repos":
            return httpx.Response(200, json=endless)
        return httpx.Response(404)

    svc, _ = _svc(handler)
    with pytest.raises(GitHubRepoError) as ei:
        svc.list_repos("acme", token=_TOKEN, base_url=_BASE)
    text = str(ei.value)
    assert "acme" in text
    assert _TOKEN not in text


def test_list_repos_sends_no_is_template_or_other_filter():
    """NO FILTER, on the wire. The unportable part of the deleted ``list_template_repos`` was its
    ``is_template`` flag — a GitHub-only concept — and this method must not reintroduce it as a
    query parameter either. What is a template is a HUMAN's statement, made by adopting a repo.

    **AN ALLOWLIST OF EXACTLY TWO, compared with ``==`` and not ``<=``.** The first version of
    this test permitted ``{"page", "per_page", "type", "sort"}`` — parameters the code does not
    send — reasoning that they are "not filters". They are: review caught that injecting
    ``type="template"`` (or ``sort``, or ``visibility``, or ``affiliation``) left every test green,
    so a build that meaning-filtered the adoption list would have shipped. ``type`` is precisely
    the GitHub-shaped filter this method refuses under a different spelling.

    A subset assertion cannot pin an ABSENCE — it only pins that nothing *unlisted* appeared, so
    every name written into the allowlist is a hole deliberately left open. Pagination is the only
    thing this request legitimately carries, so the allowlist is those two and equality is what
    makes a third addition fail."""
    seen: dict[str, str] = {}

    def handler(req):
        if req.url.path == "/orgs/acme/repos":
            seen.update(dict(req.url.params))
            return httpx.Response(200, json=[{"name": "r"}])
        return httpx.Response(404)

    svc, _ = _svc(handler)
    svc.list_repos("acme", token=_TOKEN, base_url=_BASE)

    assert set(seen) == {"page", "per_page"}, (
        f"list_repos may send PAGINATION ONLY — any other parameter is a filter, whatever it is "
        f"called ('type', 'sort', 'visibility', 'affiliation', 'is_template' are all one). "
        f"Sent: {seen}"
    )


def test_list_repos_raises_a_safe_message_on_a_403():
    """A rate limit or a missing permission must not read as "the org has no repositories" — the
    reconcile surface would then offer to create templates that already exist."""
    svc, _ = _svc(_pages_handler([], status=403))
    with pytest.raises(GitHubRepoError) as ei:
        svc.list_repos("acme", token=_TOKEN, base_url=_BASE)
    text = str(ei.value)
    assert "403" in text and "acme" in text
    assert _TOKEN not in text


def test_list_repos_raises_on_a_2xx_that_is_not_a_list():
    """A 200 carrying an object instead of an array is a provider surprise. Answering ``[]``
    would be an empty org that is not empty — and on the reconcile surface an empty org means
    "nothing to adopt, seed the starter templates", i.e. a write proposed on a wrong read.

    The check is also what keeps the failure inside this module's contract: iterating a dict
    yields its KEYS, so without it every string key becomes an ``AttributeError`` on ``.get`` —
    a type no caller catches."""

    def handler(req):
        return httpx.Response(200, json={"message": "not a list"})

    svc, _ = _svc(handler)
    with pytest.raises(GitHubRepoError) as ei:
        svc.list_repos("acme", token=_TOKEN, base_url=_BASE)
    assert "unusable" in str(ei.value)
    assert _TOKEN not in str(ei.value)


@pytest.mark.parametrize("item", [{"id": 1}, {"name": ""}, {"name": None}, "a string", None])
def test_list_repos_raises_on_an_ITEM_that_names_no_repository(item):
    """A repository entry with no usable ``name`` raises rather than being SKIPPED.

    Skipping is the tempting shape and it is the wrong one: this list is what an operator
    subtracts AGP's known repos from, so a dropped entry makes a repo that already exists look
    adoptable — the reconcile surface would offer to create something that is there. A short list
    is the same silent partial ``read_tree`` refuses. Written because mutation showed the check
    had no test of its own."""

    def handler(req):
        if req.url.path == "/orgs/acme/repos":
            return httpx.Response(200, json=[{"name": "fine"}, item])
        return httpx.Response(404)

    svc, _ = _svc(handler)
    with pytest.raises(GitHubRepoError) as ei:
        svc.list_repos("acme", token=_TOKEN, base_url=_BASE)
    assert "acme" in str(ei.value)
    assert _TOKEN not in str(ei.value)


def test_list_repos_transport_error_is_safe_and_tokenless():
    def handler(req):
        raise httpx.ConnectError("boom")

    svc, _ = _svc(handler)
    with pytest.raises(GitHubRepoError) as ei:
        svc.list_repos("acme", token=_TOKEN, base_url=_BASE)
    text = str(ei.value)
    assert _TOKEN not in text and "boom" not in text


def test_commit_files_on_head_overlays_and_refuses_a_moved_branch():
    """The overlay keeps every other path; a stale expected head writes nothing."""
    from services.repo_provider import RepoConflictError

    p = _FakeProvider()
    head = p.commit_files("acme", "agent", _TEMPLATE, branch="main", message="seed", token="t")
    new = p.commit_files_on_head(
        "acme",
        "agent",
        {"evals/evals.yaml": b"v\n"},
        branch="main",
        expected_head_sha=head,
        message="evals",
        token="t",
    )
    assert p.read_tree("acme", "agent", ref=new, token="t") == {**_TEMPLATE, "evals/evals.yaml": b"v\n"}
    assert p.read_file("acme", "agent", "evals/evals.yaml", ref=new, token="t") == b"v\n"
    assert p.read_file("acme", "agent", "nope", ref=new, token="t") is None
    with pytest.raises(RepoConflictError):
        p.commit_files_on_head(
            "acme",
            "agent",
            {"evals/evals.yaml": b"w\n"},
            branch="main",
            expected_head_sha=head,
            message="evals",
            token="t",
        )
