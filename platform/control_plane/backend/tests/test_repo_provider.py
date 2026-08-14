"""The portable ``RepoProvider`` seam and its ONE adapter (E28B/T1 — D-B1).

Three layers, each answering a question the others cannot:

**Layer 1 — the Protocol is really a contract.** A ``Protocol`` is satisfied structurally and
is NOT enforced at runtime, so a parameter renamed on the adapter would leave a class that
still "conforms" and that no portable caller can call. The signatures are therefore compared
parameter by parameter (name, kind, default), not merely by ``isinstance``.

**Layer 2 — the semantics, over ``_FakeGit``.** Reused verbatim from ``test_project_service``
(E28A/T3) rather than re-written: it already models per-branch trees, CONTENT-based tree shas,
compare and a real 3-way merge. E28 shipped divergent trees to production three times behind a
fake that had no branch dimension, so a fake more generous than reality is the specific failure
mode this import exists to avoid. The one-commit and idempotence claims are stated against that
model, where a per-path write is visible as a separate commit.

**Layer 3 — the adapter's actual HTTP.** Over ``httpx.MockTransport``, because "ONE commit" is a
claim about the git-data sequence (blobs→tree→commit→ref) and only the wire can falsify it.
This is where the request COUNTS are asserted: a second ``POST /git/commits`` is exactly the
regression the interface exists to prevent, and no in-memory fake can see one.

The token appears in no assertion as an expected substring and in several as a forbidden one —
this module's security idiom is that a message never carries it.
"""

from __future__ import annotations

import base64
import dataclasses
import inspect
import json
from pathlib import Path

import httpx
import pytest

from models.repository import PullRequestView
from services.github_repo_service import GitHubRepoError, GitHubRepoService
from services.repo_provider import RepoProvider, RepoView

# E28A/T3's git model, imported rather than mirrored. See the module docstring.
from test_project_service import _FakeGit

_BASE = "https://gh.example"
_TOKEN = "ghs_never_in_a_message"

_THE_EIGHT = (
    "create_repo",
    "commit_files",
    "set_ci_vars",
    "ensure_pipeline",
    "read_pr",
    # E28C/T1 — D-C1's three portable noun-READS.
    "read_tree",
    "read_repo",
    "list_repos",
)


def _svc(handler):
    """A service over MockTransport plus an ordered ``(method, path)`` log.

    The log is the ONLY way the one-commit claim is testable: it is a count, not a state."""
    calls: list[tuple[str, str]] = []

    def _wrapped(req: httpx.Request) -> httpx.Response:
        calls.append((req.method, req.url.path))
        return handler(req)

    return GitHubRepoService(client=httpx.Client(transport=httpx.MockTransport(_wrapped))), calls


# ===========================================================================
# Layer 1 — the Protocol IS the contract
# ===========================================================================


def test_the_protocol_has_exactly_the_eight_methods():
    """EIGHT, not seven and not nine (D-B1, amended by D-C1).

    Locked deliberately. Four was the tempting original shape — ``ensure_pipeline`` is a no-op on
    three of the four providers — and the design records that it would have hidden Azure DevOps's
    asymmetry until mid-implementation. A NINTH method is the other failure: it is how a
    provider-specific verb (``create_environment``, a GitHub-only concept) creeps back into a
    portable seam.

    **Why this test moved from five to eight (E28C/T1, D-C1 is the recorded justification).**
    The number is not a budget to be topped up — the bar the additions cleared is that all three
    are portable noun-READS, implementable on GitHub, GitLab, Bitbucket and Azure DevOps with no
    premium tier and no provider-specific concept in the signature. ``read_tree`` is the inverse
    of ``commit_files`` (same ``dict[path, bytes]``), ``read_repo`` the inverse of
    ``create_repo``, ``list_repos`` a plain enumeration. Notably ``list_repos`` takes NO filter:
    the unportable part of the ``list_template_repos`` E28B deleted was its ``is_template`` flag,
    a GitHub concept, and none returns here (see
    :func:`test_the_pre_e28b_github_shaped_methods_are_GONE`, still pinning that name as gone).
    A ninth addition needs the same argument written down before it is made."""
    declared = {
        name
        for name, member in vars(RepoProvider).items()
        if not name.startswith("_") and callable(member)
    }
    assert declared == set(_THE_EIGHT)


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
    for name in _THE_EIGHT:
        want = inspect.signature(getattr(RepoProvider, name))
        got = inspect.signature(getattr(GitHubRepoService, name))
        want_params = list(want.parameters.values())
        got_params = list(got.parameters.values())
        assert [p.name for p in got_params] == [p.name for p in want_params], name
        assert [p.kind for p in got_params] == [p.kind for p in want_params], name
        assert [p.default for p in got_params] == [p.default for p in want_params], name


def test_the_pre_e28b_github_shaped_methods_are_GONE():
    """E28B/T7 — this test is the INVERSE of the one T1 wrote here, and the inversion is the point.

    T1 added the five portable verbs ALONGSIDE the GitHub-shaped methods and pinned the old names
    as still-present, because a method deleted before its caller is a broken intermediate state.
    T2/T3/T4 moved every caller; T7 deleted the methods. Flipping the assertion rather than
    dropping it keeps a fence where there was one: each name below was a SECOND write to a
    repository the provider was already writing to, and a callable path back to that shape is
    exactly how the four-defect race returns. A re-added `create_environment` would also silently
    re-widen the App's permission surface (`github_app_manifest`'s audit) with nothing objecting.

    Reads the CLASS, not the source text: a source grep is satisfied by a comment or a dead
    branch (this epic hit that), while ``getattr`` can only be satisfied by a real attribute."""
    for name in [
        "list_template_repos",
        "generate_from_template",
        "create_branch",
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

    These four are NOT the GitHub-shaped predecessors: ``create_repo_from_zip`` still pushes
    template + infra repos (``template_rollout_service``), ``delete_repo`` backs both the catalog
    delete and the E23 cascade, ``repo_exists`` backs the delete-preview probe, and
    ``set_default_branch``/``delete_branch`` are D-B5's trunk adoption. A sweep that took any of
    them would break a live path that no Protocol method covers."""
    for name in [
        "create_repo_from_zip",
        "delete_repo",
        "repo_exists",
        "set_default_branch",
        "delete_branch",
    ]:
        assert callable(getattr(GitHubRepoService, name, None)), f"T7 must not remove {name}"


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
    """``None`` IS SUCCESS (D-B1), and a no-op spends no round trip.

    GitHub Actions registers the committed workflow itself, so there is nothing to create. Two
    claims, both load-bearing: the return is ``None`` (a caller that raised on a falsy value
    would break GitHub, GitLab AND Bitbucket while looking correct against Azure DevOps), and NO
    HTTP happens — a "no-op" that still called GitHub would be a latency and a rate-limit cost
    on every materialize, and would fail when the token lacked a scope it never needed."""
    def handler(req):  # pragma: no cover — reaching this IS the failure
        raise AssertionError(f"ensure_pipeline must issue no request, sent {req.method} {req.url}")

    svc, calls = _svc(handler)
    assert svc.ensure_pipeline("acme", "r", yaml_path=".github/workflows/build.yml",
                               token=_TOKEN, base_url=_BASE) is None
    assert calls == []


# ===========================================================================
# Layer 2 — the semantics, stated over _FakeGit
# ===========================================================================


class _FakeProvider:
    """A ``RepoProvider`` over E28A/T3's :class:`_FakeGit`.

    It implements ``commit_files`` on ``_FakeGit._commit`` — the same primitive ``_FakeGit.write``
    itself uses — rather than by looping ``write``, because looping ``write`` would produce N
    commits for N files and the one-commit contract would then be untestable here. Nothing in
    the model is loosened: the tree stays per-branch (a write to `dev` is invisible on `main`),
    the tree sha stays CONTENT-derived, and the idempotence gate below compares trees through
    ``_FakeGit.tree`` instead of re-deriving a hash this class does not own.

    ``set_ci_vars`` records; ``ensure_pipeline`` returns ``None`` (this fake stands in for a
    provider that auto-registers); ``read_pr`` answers a fixed view. ``commit_files`` and — since
    E28C/T1 — ``read_tree`` are the two methods carrying real behaviour, because they are the two
    with semantics a fake can express: they are INVERSES over the same model, which is the
    property the round-trip test below states. The rest are HTTP shapes, and those are Layer 3's
    job.

    ``read_tree`` reads through ``_FakeGit`` by REF, not by branch name, so a mid-read push
    cannot yield a mixed tree — the reason D-C1 makes ``ref`` required. ``_FakeGit`` mints commit
    shas, so a ref here is a commit sha and the tree is the snapshot that sha carries.
    """

    def __init__(self):
        self.git = _FakeGit()
        self.repos: list[tuple[str, str, bool]] = []
        self.ci_vars: dict[str | None, dict[str, str]] = {}
        self.commit_messages: list[str] = []
        # Every branch this provider actually PUSHED, in order. A no-op re-run must add
        # NOTHING here: a push is what fires a build, so "no commit" and "no push" are one
        # claim and a fake that logged the attempt could not tell them apart.
        self.pushes: list[str] = []

    def create_repo(self, org, name, *, private, token, base_url=None):
        self.repos.append((org, name, private))
        return f"https://github.com/{org}/{name}"

    def read_tree(self, org, repo, *, ref, token, base_url=None):
        if not ref:
            raise GitHubRepoError("read_tree requires a ref")
        if ref not in self.git.commits:
            raise GitHubRepoError(f"no such ref '{ref}'")
        # A COPY: a caller that mutates what it got must not edit the repository's history.
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
            # THE IDEMPOTENCE GATE, in the model's own terms: identical content is the tree
            # already on the branch, so no commit and no push.
            return self.git.branches[branch]
        parents = [self.git.branches[branch]] if branch in self.git.branches else []
        sha = self.git._commit(tree, parents)
        self.git.branches[branch] = sha
        self.commit_messages.append(message)
        self.pushes.append(branch)
        return sha

    def set_ci_vars(self, org, repo, variables, *, scope, token, base_url=None):
        self.ci_vars[scope] = {k: v for k, v in variables.items() if v}

    def ensure_pipeline(self, org, repo, *, yaml_path, token, base_url=None):
        return None

    def read_pr(self, org, repo, number, *, token, base_url=None):
        return PullRequestView(
            number=number, title="t", state="open", author="someone", head_sha="s",
            url="u", can_approve=False, approve_blocked_reason="no viewer", mergeable=None,
        )


_TEMPLATE = {
    "src/main.py": b"# the agent\n",
    ".github/workflows/build.yml": b"name: build\n",
    "requirements.txt": b"strands\n",
}


def test_the_fake_provider_satisfies_the_protocol():
    """The test double is held to the SAME contract as the adapter.

    A double that drifted from the interface would let the semantic tests below pass against a
    shape no real provider has — which is the E28A lesson restated for the fake."""
    assert isinstance(_FakeProvider(), RepoProvider)


def test_commit_files_writes_one_commit_for_n_files():
    """N files, ONE commit — the property the whole epic rests on.

    Six writes to a fresh repo is what raced GitHub's asynchronous template copy four separate
    times. One commit also means ONE CI trigger, so a single ``[skip ci]`` suppresses the
    materialize build with no branch filter to keep correct."""
    p = _FakeProvider()
    p.commit_files("acme", "r", _TEMPLATE, branch="main",
                   message="chore: initialize from template [skip ci]", token=_TOKEN)

    assert len(p.git.commits) == 1
    assert p.git.tree("main") == _TEMPLATE
    assert p.pushes == ["main"]


def test_commit_files_is_a_no_op_when_the_content_is_identical():
    """A RETRIED materialize must converge, not append.

    Identical content is the tree already on the branch, so the second call writes no commit and
    fires no push. Both halves matter: an empty commit would still be a PUSH, and a push on an
    agent repo is a BUILD — E28A shipped exactly that (a config commit on `main` registered a
    prod candidate on a repo nobody had merged to)."""
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

    p.commit_files("acme", "r", {"src/main.py": b"# only this\n"}, branch="main",
                   message="m2", token=_TOKEN)

    assert p.git.tree("main") == {"src/main.py": b"# only this\n"}


def test_commit_files_to_one_branch_is_invisible_on_another():
    """Per-branch trees — the dimension the pre-E28A fake lacked.

    A write to `dev` that showed up on `main` is precisely how the divergent-tree defects
    passed their tests. Asserted here so this module inherits the property rather than
    assuming it."""
    p = _FakeProvider()
    p.commit_files("acme", "r", _TEMPLATE, branch="main", message="m", token=_TOKEN)
    p.commit_files("acme", "r", dict(_TEMPLATE, **{"extra.txt": b"x"}), branch="dev",
                   message="m2", token=_TOKEN)

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
    """The property the whole epic turns on (D-C1): what ``commit_files`` wrote,
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
    """``ref`` IS THE POINT (D-C1): a read pinned to a sha cannot yield a mixed tree.

    Materialize resolves a head sha, then reads at it. If ``read_tree`` tracked the BRANCH
    instead, a template author pushing mid-materialize would have half their old template and
    half their new one land in a customer's repo — a tree that never existed as a commit. The
    older ref must still answer the older bytes."""
    p = _FakeProvider()
    first = p.commit_files("acme", "r", _TEMPLATE, branch="main", message="m", token=_TOKEN)
    second = p.commit_files("acme", "r", {"src/main.py": b"# rewritten\n"}, branch="main",
                            message="m2", token=_TOKEN)

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
    """``None`` is NOT-FOUND and nothing else (D-C1). It is the whole basis of the three-state
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
    # ``FrozenInstanceError`` specifically — a bare ``Exception`` would also be satisfied by an
    # ``AttributeError`` from a typo in the attribute name, i.e. by the test being wrong.
    with pytest.raises(dataclasses.FrozenInstanceError):
        view.head_sha = "def456"  # type: ignore[misc]


# ===========================================================================
# Layer 3 — the GitHub adapter's actual HTTP
# ===========================================================================


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
            return httpx.Response(422, json={
                "message": "Repository creation failed.",
                "errors": [{"message": "name already exists on this account"}],
            })
        if req.method == "GET" and req.url.path == "/repos/acme/r":
            return httpx.Response(200, json=_created(html_url="https://github.com/acme/r"))
        return httpx.Response(404)

    svc, calls = _svc(handler)
    assert svc.create_repo("acme", "r", private=True, token=_TOKEN,
                           base_url=_BASE) == "https://github.com/acme/r"
    assert ("GET", "/repos/acme/r") in calls


def test_create_repo_fails_closed_on_every_other_422():
    """422 is OVERLOADED on this endpoint. Only "already exists" is benign; a rejected name or
    a quota refusal must still raise, or a materialize proceeds against a repo that is not
    there. The message carries GitHub's own validation string (a safe one) and NEVER the token."""
    def handler(req):
        return httpx.Response(422, json={
            "message": "Repository creation failed.",
            "errors": [{"message": "name is too long (maximum is 100 characters)"}],
        })

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
    import hashlib

    return f"blob-{hashlib.sha256(content).hexdigest()[:12]}"


def _git_data_handler(*, tip=None, tip_tree=None, tree_sha="tree-new", commit_sha="commit-new",
                      seen_tree=None):
    """A git-data backend for ``org/r``: N blobs, one tree, one commit, one ref write.

    ``tip``/``tip_tree`` describe the branch's existing state — ``None`` means the branch does
    not exist, which is the 404 the adapter reads as "new branch".

    ``seen_tree`` — when a dict is passed, the tree POST records ``{path: blob_sha}`` into it, so
    a test can pin WHICH path carried WHICH content's sha. Without that the request was only
    counted, never inspected."""
    def handler(req):
        p, m = req.url.path, req.method
        if m == "GET" and p == "/repos/acme/r/git/ref/heads/main":
            if tip is None:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json={"object": {"sha": tip}})
        if m == "GET" and p == f"/repos/acme/r/git/commits/{tip}":
            return httpx.Response(200, json={"sha": tip, "tree": {"sha": tip_tree}})
        if m == "POST" and p == "/repos/acme/r/git/blobs":
            body = json.loads(req.content)
            assert body["encoding"] == "base64"
            # The sha is derived from the DECODED content, so it is a real answer about what was
            # sent rather than a label the handler chose. base64 round-trips exactly, so a
            # corrupted body cannot produce the sha of the intended content.
            return httpx.Response(
                201, json={"sha": _blob_sha(base64.b64decode(body["content"]))}
            )
        if m == "POST" and p == "/repos/acme/r/git/trees":
            body = json.loads(req.content)
            # NO base_tree — the resulting tree is EXACTLY the pushed files.
            assert "base_tree" not in body
            assert all(i["mode"] == "100644" and i["type"] == "blob" for i in body["tree"])
            if seen_tree is not None:
                for item in body["tree"]:
                    seen_tree[item["path"]] = item["sha"]
            return httpx.Response(201, json={"sha": tree_sha})
        if m == "POST" and p == "/repos/acme/r/git/commits":
            return httpx.Response(201, json={"sha": commit_sha})
        if m == "PATCH" and p == "/repos/acme/r/git/refs/heads/main":
            body = json.loads(req.content)
            assert body["sha"] == commit_sha and body["force"] is True
            return httpx.Response(200, json={})
        if m == "POST" and p == "/repos/acme/r/git/refs":
            body = json.loads(req.content)
            assert body == {"ref": "refs/heads/main", "sha": commit_sha}
            return httpx.Response(201, json={})
        return httpx.Response(404, json={"message": f"unexpected {m} {p}"})

    return handler


def test_commit_files_issues_exactly_one_commit_for_three_files():
    """The one-commit claim, ON THE WIRE — where a second commit is countable.

    Three blobs, ONE tree, ONE commit, ONE ref write. No in-memory fake can falsify this: the
    regression it guards is an extra ``POST /git/commits``, which is a request, not a state."""
    svc, calls = _svc(_git_data_handler(tip="tip-sha", tip_tree="tree-old"))
    sha = svc.commit_files("acme", "r", _TEMPLATE, branch="main",
                           message="chore: initialize from template [skip ci]",
                           token=_TOKEN, base_url=_BASE)

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
    svc.commit_files("acme", "r", _TEMPLATE, branch="main", message="m",
                     token=_TOKEN, base_url=_BASE)

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
    svc.commit_files("acme", "r", _TEMPLATE, branch="main", message="m",
                     token=_TOKEN, base_url=_BASE)

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
        svc.commit_files("acme", "r", _TEMPLATE, branch="main", message="m",
                         token=_TOKEN, base_url=_BASE)
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
    svc.commit_files("acme", "r", _TEMPLATE, branch="main",
                     message="chore: initialize from template [skip ci]",
                     token=_TOKEN, base_url=_BASE)

    assert seen["message"] == "chore: initialize from template [skip ci]"
    assert seen["parents"] == ["tip-sha"]
    assert seen["tree"] == "tree-new"


def test_commit_files_writes_nothing_when_the_tree_sha_already_matches():
    """IDEMPOTENT BY CONTENT — the mechanism, not a promise.

    Git objects are content-addressed, so re-posting identical files yields the SAME tree sha.
    When that sha is already the tip's tree the adapter writes NO commit and does NOT move the
    ref, and the existing tip sha is returned. No ref write means no push event, hence no
    duplicate build — which is the half that actually made E28A's retry unsafe."""
    svc, calls = _svc(_git_data_handler(tip="tip-sha", tip_tree="tree-new", tree_sha="tree-new"))
    sha = svc.commit_files("acme", "r", _TEMPLATE, branch="main", message="m",
                           token=_TOKEN, base_url=_BASE)

    assert sha == "tip-sha", "the existing tip is the answer, not a fresh commit"
    assert ("POST", "/repos/acme/r/git/commits") not in calls
    assert ("PATCH", "/repos/acme/r/git/refs/heads/main") not in calls


def test_commit_files_creates_the_branch_when_it_does_not_exist():
    """A 404 on the ref is "the branch is new", and the adapter CREATES it — a root commit
    (``parents: []``) plus ``POST /git/refs``.

    That keeps trunk naming the project's business (D-B2) rather than requiring the trunk to be
    whatever ``auto_init`` happened to name."""
    seen = {}

    def handler(req):
        if req.method == "POST" and req.url.path == "/repos/acme/r/git/commits":
            seen.update(json.loads(req.content))
        return _git_data_handler(tip=None)(req)

    svc, calls = _svc(handler)
    sha = svc.commit_files("acme", "r", _TEMPLATE, branch="main", message="m",
                           token=_TOKEN, base_url=_BASE)

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
        svc.commit_files("acme", "r", _TEMPLATE, branch="main", message="m",
                         token=_TOKEN, base_url=_BASE)
    assert "500" in str(ei.value)
    assert _TOKEN not in str(ei.value)
    assert ("POST", "/repos/acme/r/git/commits") not in calls


def test_commit_files_refuses_an_empty_file_set_before_any_request():
    """An empty mapping would build an EMPTY tree — deleting the branch's whole contents.
    Refused BEFORE any HTTP, so a caller with nothing to push cannot destroy a repo by
    accident."""
    def handler(req):  # pragma: no cover — reaching this IS the failure
        raise AssertionError("must refuse before issuing a request")

    svc, calls = _svc(handler)
    with pytest.raises(GitHubRepoError) as ei:
        svc.commit_files("acme", "r", {}, branch="main", message="m",
                         token=_TOKEN, base_url=_BASE)
    assert "empty" in str(ei.value)
    assert calls == []


def test_commit_files_transport_error_is_safe_and_tokenless():
    def handler(req):
        raise httpx.ReadTimeout("slow")

    svc, _ = _svc(handler)
    with pytest.raises(GitHubRepoError) as ei:
        svc.commit_files("acme", "r", _TEMPLATE, branch="main", message="m",
                         token=_TOKEN, base_url=_BASE)
    text = str(ei.value)
    assert _TOKEN not in text and "slow" not in text
    assert "ReadTimeout" in text


def test_set_ci_vars_with_no_scope_writes_the_repository_wide_set():
    """``scope=None`` ⇒ the repo-level Actions ``vars``. This is the ONLY scope E28B's
    materialize uses: a named one must already exist, and creating it would be a second write."""
    written = {}

    def handler(req):
        if req.method == "POST" and req.url.path == "/repos/acme/r/actions/variables":
            body = json.loads(req.content)
            written[body["name"]] = body["value"]
            return httpx.Response(201, json={})
        return httpx.Response(404)

    svc, _ = _svc(handler)
    svc.set_ci_vars("acme", "r", {"AWS_REGION": "us-east-1", "AGENT_ID": "a1"},
                    scope=None, token=_TOKEN, base_url=_BASE)

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
    svc.set_ci_vars("acme", "r", {"X": "1"}, scope="staging-eu",
                    token=_TOKEN, base_url=_BASE)

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
    cannot tell an empty string from a deliberate blank — E28A's ``LANGFUSE_SECRET_NAME``
    reached the container as exactly that and tracing could not authenticate."""
    names = []

    def handler(req):
        if req.method == "POST":
            names.append(json.loads(req.content)["name"])
            return httpx.Response(201, json={})
        return httpx.Response(404)

    svc, _ = _svc(handler)
    svc.set_ci_vars("acme", "r", {"KEEP": "v", "BLANK": "", "MISSING": None},
                    scope=None, token=_TOKEN, base_url=_BASE)

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


def _pr_body(**overrides):
    body = {
        "number": 7,
        "title": "Add the thing",
        "state": "closed",
        "user": {"login": "lars-svensson"},
        "head": {"sha": "head-sha"},
        "html_url": "https://github.com/acme/r/pull/7",
        "merged_at": "2026-08-01T10:00:00Z",
    }
    body.update(overrides)
    return body


def test_read_pr_reports_a_merged_pull_request_as_merged():
    """``state`` is DERIVED, not passed through: GitHub calls a merged PR "closed", and on this
    surface merged is the interesting case — it is what registers the prod candidate an Owner
    then promotes. Reporting it as merely closed would lose the one distinction that matters."""
    def handler(req):
        if req.method == "GET" and req.url.path == "/repos/acme/r/pulls/7":
            return httpx.Response(200, json=_pr_body())
        return httpx.Response(404)

    svc, _ = _svc(handler)
    view = svc.read_pr("acme", "r", 7, token=_TOKEN, base_url=_BASE)

    assert isinstance(view, PullRequestView)
    assert view.state == "merged"
    assert view.number == 7 and view.author == "lars-svensson" and view.head_sha == "head-sha"


def test_read_pr_never_offers_an_approval_through_the_portable_seam():
    """FAIL CLOSED on approval standing (D15).

    The portable contract carries no ``viewer_login``, so this method cannot establish whose
    account a review would be taken as. ``can_approve`` is therefore always ``False`` with a
    stated reason — an approve button belongs to ``GitHubPrService`` under the human's own
    token. A ``True`` here would offer a self-approval GitHub then refuses, under AGP's OWN App
    token, which is precisely the reviewer independence the gate exists to provide."""
    def handler(req):
        return httpx.Response(200, json=_pr_body(state="open", merged_at=None))

    svc, _ = _svc(handler)
    view = svc.read_pr("acme", "r", 7, token=_TOKEN, base_url=_BASE)

    assert view.state == "open"
    assert view.can_approve is False
    assert view.approve_blocked_reason


def test_read_pr_leaves_an_uncomputed_mergeable_as_none_not_false():
    """``None`` ⇒ nobody established mergeability. NOT ``False``, which is the reading that
    suppresses a merge that would have succeeded (E28 finding #7)."""
    def handler(req):
        return httpx.Response(200, json=_pr_body(state="open", merged_at=None, mergeable=None))

    svc, _ = _svc(handler)
    assert svc.read_pr("acme", "r", 7, token=_TOKEN, base_url=_BASE).mergeable is None


def test_read_pr_raises_on_a_2xx_that_is_not_a_pull_request():
    """A 200 whose body is not a pull request is a provider surprise. Answering a fabricated
    view would be worse than saying so."""
    def handler(req):
        return httpx.Response(200, json={"unexpected": True})

    svc, _ = _svc(handler)
    with pytest.raises(GitHubRepoError) as ei:
        svc.read_pr("acme", "r", 7, token=_TOKEN, base_url=_BASE)
    assert _TOKEN not in str(ei.value)


def test_read_pr_raises_a_safe_message_on_a_404():
    def handler(req):
        return httpx.Response(404, json={"message": "Not Found"})

    svc, _ = _svc(handler)
    with pytest.raises(GitHubRepoError) as ei:
        svc.read_pr("acme", "r", 7, token=_TOKEN, base_url=_BASE)
    text = str(ei.value)
    assert "404" in text and "#7" in text
    assert _TOKEN not in text


# ===========================================================================
# Layer 3 (cont.) — E28C/T1's three reads, on the wire (D-C1)
#
# ``read_tree`` is walked over the git-data API (``git/trees?recursive=1`` then one
# ``git/blobs/{sha}`` per file) rather than pulled as a tarball, and that choice is what makes
# the tests below possible to write honestly: the trees response STATES its truncation
# (``truncated: true``) and STATES each entry's kind (``mode``/``type``), so "raise on a partial"
# and "raise on a symlink or submodule" are assertions about a flag the provider actually sends.
# A tarball reports neither — a git archive silently OMITS submodules and a tar cut at a member
# boundary just ends — so those two pins would have been untestable claims.
#
# THE FAKE KEYS BLOBS ON CONTENT, never on a path or a length. ``_blob_sha`` above records why:
# under a length-keyed fake, serving one file's bytes under another's sha was invisible, and
# three mutants survived. Here the blob endpoint is a sha→content MAP, so a read that returned
# the wrong blob for a path cannot produce the right bytes.
# ===========================================================================


def _tree_entry(path, content, mode="100644", type_="blob"):
    return {"path": path, "mode": mode, "type": type_, "sha": _blob_sha(content), "size": len(content)}


def _tree_handler(*, files=None, truncated=False, extra_entries=(), ref="head-sha",
                  blob_overrides=None, blobs_seen=None):
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
            return httpx.Response(200, json={
                "sha": sha,
                "encoding": "base64",
                "content": base64.b64encode(by_sha[sha]).decode("ascii"),
            })
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
    # Both blobs are reachable at all times — the repo really does hold both objects, so a
    # misdirected read SUCCEEDS with the wrong bytes instead of erroring.
    by_sha = {_blob_sha(old_bytes): old_bytes, _blob_sha(new_bytes): new_bytes}

    def handler(req):
        p, m = req.url.path, req.method
        if m == "GET" and p in ("/repos/acme/r/git/trees/old-sha", "/repos/acme/r/git/trees/new-sha"):
            content = old_bytes if p.endswith("old-sha") else new_bytes
            return httpx.Response(200, json={
                "tree": [_tree_entry("src/main.py", content)], "truncated": False,
            })
        if m == "GET" and p.startswith("/repos/acme/r/git/blobs/"):
            sha = p.rsplit("/", 1)[-1]
            return httpx.Response(200, json={
                "sha": sha, "encoding": "base64",
                "content": base64.b64encode(by_sha[sha]).decode("ascii"),
            })
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
    def handler(req):  # pragma: no cover — reaching this IS the failure
        raise AssertionError(f"must refuse a blank ref before any request, sent {req.url}")

    svc, calls = _svc(handler)
    with pytest.raises(GitHubRepoError) as ei:
        svc.read_tree("acme", "r", ref="", token=_TOKEN, base_url=_BASE)
    assert "ref" in str(ei.value)
    assert calls == []


@pytest.mark.parametrize("evil_ref", [
    "../../../orgs/other-org/repos",   # walks OUT of /repos/{org}/{repo}/git/trees entirely
    "a/../../b",                        # a mid-path walk
    "/refs/heads/main",                 # absolute — collapses the {org}/{repo} segments
    "%2e%2e/%2e%2e/orgs/other-org",     # the traversal PERCENT-ENCODED (E28C final review)
    "..%2f..%2forgs/other-org",         # only the separators encoded
    "a/..%2f%2e%2e/b",                  # mixed: a literal `..` segment beside encoded ones
])
def test_read_tree_refuses_a_ref_that_would_REDIRECT_the_request(evil_ref):
    """A ``ref`` is interpolated into the URL, so it must not be able to STEER the request.

    ``httpx`` normalises ``..`` in a path before sending, exactly as a browser does — verified
    directly: ``/repos/acme/r/git/trees/../../../orgs/evil/repos`` leaves as
    ``/repos/acme/orgs/evil/repos``. So a ref carrying a traversal does not 404; it asks GITHUB A
    DIFFERENT QUESTION, under AGP's own App installation token, and this method would return
    whatever that endpoint answered as though it were a template's file tree.

    THE PERCENT-ENCODED FORMS ARE THE SAME ATTACK WITH THE GUARD'S LITERAL ``..`` HIDDEN (found by
    E28C's final review, which executed them). ``httpx`` does NOT decode ``%2e%2e`` — it sends the
    escape through literally — so a guard matching only literal ``..`` lets the request onto the
    wire and refusal becomes a bet on GitHub's server-side decoding. That is precisely the
    dependency this guard exists to refuse, so ``%`` is refused outright: it is not legal in a git
    ref name, so no legitimate ref carries one.

    That matters because ``ref`` is NOT operator-typed: from T3/T4 it arrives from a DynamoDB
    template record and from a provider response. E28B's final review closed the same class of
    hole on the disk path (``_resolve_scaffold_dir``); this is its URL counterpart, and it is
    refused rather than escaped — a caller with a malformed ref has a bug worth being told about.

    No request is issued, so the refusal cannot be confused with a provider answer."""
    def handler(req):  # pragma: no cover — reaching this IS the failure
        raise AssertionError(f"a traversing ref must be refused before any request: {req.url}")

    svc, calls = _svc(handler)
    with pytest.raises(GitHubRepoError) as ei:
        svc.read_tree("acme", "r", ref=evil_ref, token=_TOKEN, base_url=_BASE)
    assert "ref" in str(ei.value)
    assert calls == [], "a traversing ref must never reach the wire"


@pytest.mark.parametrize("evil_ref", [
    "main?per_page=1",       # ADDS a query parameter to AGP's request
    "main?recursive=0",      # aims at the one parameter this method depends on
    "main#fragment",         # silently truncates the ref to `main` — a DIFFERENT ref, no error
])
def test_read_tree_refuses_a_ref_that_would_inject_a_QUERY_or_a_FRAGMENT(evil_ref):
    """A ref may not carry a ``?`` or a ``#`` — the other half of "a ref must not steer the
    request", found in review after the traversal guard landed.

    Both were verified against ``httpx`` directly, and they fail in two different ways:

    * ``ref="main?per_page=1"`` is sent as path ``…/git/trees/main`` with ``per_page=1`` MERGED
      into the query beside AGP's own ``recursive=1``. The ref is no longer the only thing the
      caller controls; ``recursive`` is the parameter this method's completeness depends on, and
      a request that quietly carries an extra parameter is one AGP did not author.
    * ``ref="main#fragment"`` is sent as ``…/git/trees/main`` with the fragment DROPPED — so the
      read silently succeeds against a ref that is not the one asked for. That is the mixed-tree
      failure this parameter exists to prevent, arriving without an error to notice.

    Refused before any request, for the same reason as the traversal: ``ref`` is not
    operator-typed (T3/T4 pass it from a DynamoDB record and from a provider response), so a
    malformed one is a bug to state rather than a value to sanitise. Neither character is legal in
    a git ref name, so nothing legitimate is turned away."""
    def handler(req):  # pragma: no cover — reaching this IS the failure
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

    This is E28B's own failure class restated: a partial that looks complete. GitHub caps a
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
    assert svc.read_tree("acme", "r", ref="head-sha", token=_TOKEN,
                         base_url=_BASE)["entrypoint.sh"] == exe


def test_read_tree_rejects_a_path_that_would_escape_the_tree_root():
    """Paths are ROOT-RELATIVE. A leading slash or a ``..`` segment must not pass through.

    What comes back here is fed straight into ``commit_files`` — and on the seed path, sibling
    code writes to disk (E28B's final review closed a traversal there). A provider that answered
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
        # ``files`` already puts the bad path in the listing AND its blob in the sha map, so a
        # reader with no path check succeeds and returns a traversing key.
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
    svc, _ = _svc(_tree_handler(blob_overrides={
        sha: httpx.Response(200, json={"sha": sha, "encoding": "base64"}),
    }))
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
    svc, _ = _svc(_tree_handler(blob_overrides={
        sha: httpx.Response(200, json={"sha": sha, "encoding": "none", "content": ""}),
    }))
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


# --------------------------------------------------------------------------- #
# The 2xx-of-the-wrong-SHAPE family, swept per entry field.
#
# This block exists because mutation testing found six guards with no test of their own — each
# was shielded by a neighbour, which is the "one guarded call site and two unguarded ones" pattern
# ``test_commit_files_surfaces_a_shaless_2xx_as_a_repo_error`` already parametrises against on the
# WRITE path. The claim under test is NOT "a malformed body is rejected" (obvious) but "the
# rejection is ``GitHubRepoError`` and never a bare ``KeyError``/``AttributeError``/``TypeError``":
# every caller of this service catches ``GitHubRepoError``, so a raw builtin escaping here means
# the materialize step wrapper that should record a failed step instead meets a type it does not
# handle, and the operator sees a 500 with no step timeline.
#
# ``pytest.raises(GitHubRepoError)`` is the assertion, and it is a real one — the builtins are NOT
# subclasses of it, so a leaked ``AttributeError`` fails the test rather than satisfying it.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("entry", [
    "not-a-dict",
    None,
    {"mode": "100644", "type": "blob", "sha": "s"},                 # no path
    {"path": "", "mode": "100644", "type": "blob", "sha": "s"},     # blank path
    {"path": "x.py", "mode": "100644", "type": "blob"},             # no sha
    {"path": "x.py", "mode": "100644", "type": "blob", "sha": ""},  # blank sha
    {"path": "x.py", "mode": "100644", "type": "blob", "sha": None},
])
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
    # The same body through the existence probe: one helper, so one answer.
    svc2, _ = _svc(handler)
    with pytest.raises(GitHubRepoError):
        svc2.repo_exists("acme", "r", _TOKEN, base_url=_BASE)


@pytest.mark.parametrize("ref_body", [{}, {"object": {}}, {"object": None},
                                      {"object": {"sha": ""}}, "not-a-dict"])
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
    this state, and E28B's ``create_repo`` produces one on every materialize, so the seam has to
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
    svc, _ = _svc(_pages_handler([[
        {"name": "strands-agentcore", "private": True, "html_url": "u", "id": 1},
        {"name": "agp-runtime-infra", "private": True, "html_url": "u2", "id": 2},
    ]]))
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
    this surface is costed against (D-C3)."""
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
    """NO FILTER, on the wire (D-C1). The unportable part of the ``list_template_repos`` E28B
    deleted was its ``is_template`` flag — a GitHub-only concept — and this method must not
    reintroduce it as a query parameter either. What is a template is a HUMAN's statement, made
    by adopting a repo (D-C4).

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
