"""The GitHub write client over ``httpx.MockTransport``: the zip-scaffold rollout path (create with
``auto_init``, then commit the scaffold onto the auto-init parent and fast-forward the branch), the
delete/probe pair, the fast-forward overlay commit, the single-file read and the branch create. The
portable verbs are covered in ``test_repo_provider``."""

import base64
import io
import json
import zipfile

import httpx
import pytest

from services.github_repo_errors import GitHubRepoConflictError, GitHubRepoError
from services.github_repo_service import GitHubRepoService
from services.repo_provider import RepoConflictError


def _zip(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for path, content in files.items():
            zf.writestr(path, content)
    return buf.getvalue()


def test_create_repo_from_zip_auto_inits_then_commits_scaffold():
    """A truly empty repo 409s on the first git/blobs call, so the repo is created ``auto_init``
    and the scaffold commit is parented on the seeded commit."""
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append((req.method, req.url.path))
        p = req.url.path
        if req.method == "POST" and p == "/orgs/acme/repos":
            body = json.loads(req.content)
            assert body["auto_init"] is True
            return httpx.Response(
                201, json={"html_url": "https://github.com/acme/tmpl", "owner": {"login": "acme"}}
            )
        if req.method == "GET" and p == "/repos/acme/tmpl/git/ref/heads/main":
            return httpx.Response(200, json={"object": {"sha": "parent-sha"}})
        if req.method == "POST" and p == "/repos/acme/tmpl/git/blobs":
            return httpx.Response(201, json={"sha": "blob-sha"})
        if req.method == "POST" and p == "/repos/acme/tmpl/git/trees":
            return httpx.Response(201, json={"sha": "tree-sha"})
        if req.method == "POST" and p == "/repos/acme/tmpl/git/commits":
            body = json.loads(req.content)
            assert body["parents"] == ["parent-sha"]
            return httpx.Response(201, json={"sha": "commit-sha"})
        if req.method == "PATCH" and p == "/repos/acme/tmpl/git/refs/heads/main":
            body = json.loads(req.content)
            assert body["sha"] == "commit-sha" and body["force"] is True
            return httpx.Response(200, json={})
        return httpx.Response(404)

    svc = GitHubRepoService(client=httpx.Client(transport=httpx.MockTransport(handler)))
    url = svc.create_repo_from_zip("acme", "tmpl", _zip({"README.md": "hi", "app/main.py": "x"}), "ghs_tok")

    assert url == "https://github.com/acme/tmpl"
    assert ("POST", "/orgs/acme/repos") == calls[0]
    assert ("GET", "/repos/acme/tmpl/git/ref/heads/main") == calls[1]
    assert ("PATCH", "/repos/acme/tmpl/git/refs/heads/main") == calls[-1]
    assert calls.count(("POST", "/repos/acme/tmpl/git/blobs")) == 2


def test_create_repo_from_zip_surfaces_github_message_and_hint():
    """GitHub's own validation message is a safe string and is surfaced, plus the re-run hint on a
    422; the token never is."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={
                "message": "Repository creation failed.",
                "errors": [{"message": "name already exists on this account"}],
            },
        )

    svc = GitHubRepoService(client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(GitHubRepoError) as ei:
        svc.create_repo_from_zip("acme", "tmpl", _zip({"f": "x"}), "ghs_tok")
    text = str(ei.value)
    assert "422" in text
    assert "name already exists" in text
    assert "already exist" in text
    assert "ghs_tok" not in text


def test_transport_error_is_safe_and_tokenless():
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    svc = GitHubRepoService(client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(GitHubRepoError) as ei:
        svc.create_repo_from_zip("acme", "tmpl", _zip({"f": "x"}), "ghs_tok")
    assert "ghs_tok" not in str(ei.value)


@pytest.mark.parametrize("shaless", ["ref", "blob", "tree", "commit"])
def test_the_zip_push_surfaces_a_shaless_2xx_as_a_repo_error(shaless):
    """A 2xx whose body lacks the sha is ``GitHubRepoError``, never a bare ``KeyError``, on every
    read of the push; a partial repair of one read must fail."""

    def handler(req: httpx.Request) -> httpx.Response:
        p = req.url.path
        if req.method == "POST" and p == "/orgs/acme/repos":
            return httpx.Response(
                201, json={"html_url": "https://github.com/acme/tmpl", "owner": {"login": "acme"}}
            )
        if req.method == "GET" and p == "/repos/acme/tmpl/git/ref/heads/main":
            return httpx.Response(200, json={} if shaless == "ref" else {"object": {"sha": "p"}})
        if req.method == "POST" and p == "/repos/acme/tmpl/git/blobs":
            return httpx.Response(201, json={} if shaless == "blob" else {"sha": "b"})
        if req.method == "POST" and p == "/repos/acme/tmpl/git/trees":
            return httpx.Response(201, json={} if shaless == "tree" else {"sha": "t"})
        if req.method == "POST" and p == "/repos/acme/tmpl/git/commits":
            return httpx.Response(201, json={} if shaless == "commit" else {"sha": "c"})
        if req.method == "PATCH" and p == "/repos/acme/tmpl/git/refs/heads/main":
            return httpx.Response(200, json={})
        return httpx.Response(404)

    svc = GitHubRepoService(client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(GitHubRepoError) as ei:
        svc.create_repo_from_zip("acme", "tmpl", _zip({"f": "x"}), "ghs_tok")
    text = str(ei.value)
    assert "no sha" in text or "no commit" in text, text
    assert "ghs_tok" not in text


_BASE = "https://gh.example"


def _svc(handler):
    """Build a service backed by a MockTransport, plus a shared call log."""
    calls = []

    def _wrapped(req: httpx.Request) -> httpx.Response:
        calls.append((req.method, req.url.path, req))
        return handler(req)

    svc = GitHubRepoService(client=httpx.Client(transport=httpx.MockTransport(_wrapped)))
    return svc, calls


def test_delete_repo_success():
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "DELETE" and req.url.path == "/repos/acme/r":
            return httpx.Response(204)
        return httpx.Response(404)

    svc, calls = _svc(handler)
    svc.delete_repo("acme", "r", "tok", base_url=_BASE)
    verbs = [(m, path) for (m, path, _req) in calls]
    assert verbs == [("DELETE", "/repos/acme/r")]


def test_delete_repo_404_is_idempotent():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    svc, _ = _svc(handler)
    svc.delete_repo("acme", "r", "tok", base_url=_BASE)


def test_delete_repo_error_is_safe():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            json={
                "message": "Server Error",
                "documentation_url": "https://docs.github.com/secret-leak",
            },
        )

    svc, _ = _svc(handler)
    with pytest.raises(GitHubRepoError) as ei:
        svc.delete_repo("acme", "r", "ghs_tok", base_url=_BASE)
    text = str(ei.value)
    assert "500" in text
    assert "ghs_tok" not in text
    assert "documentation_url" not in text


def test_repo_exists_true_on_200():
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path == "/repos/acme/r":
            return httpx.Response(200, json={"name": "r"})
        return httpx.Response(404)

    svc, calls = _svc(handler)
    assert svc.repo_exists("acme", "r", "tok", base_url=_BASE) is True
    verbs = [(m, path) for (m, path, _req) in calls]
    assert verbs == [("GET", "/repos/acme/r")]


def test_repo_exists_false_on_404():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    svc, _ = _svc(handler)
    assert svc.repo_exists("acme", "r", "tok", base_url=_BASE) is False


def test_repo_exists_error_is_safe():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            json={
                "message": "Server Error",
                "documentation_url": "https://docs.github.com/secret-leak",
            },
        )

    svc, _ = _svc(handler)
    with pytest.raises(GitHubRepoError) as ei:
        svc.repo_exists("acme", "r", "ghs_tok", base_url=_BASE)
    text = str(ei.value)
    assert "500" in text
    assert "ghs_tok" not in text
    assert "documentation_url" not in text


def test_actions_review_methods_removed():
    """The GitHub-Actions-only deployment-review surface is deleted, not shimmed: prod promotion
    is an AGP action, and re-adding any of these would reintroduce a provider-specific approval
    path."""
    for gone in ("list_waiting_runs", "list_pending_deployments", "review_deployment"):
        assert not hasattr(GitHubRepoService, gone)


def _git_handler(calls, *, tip="head-sha", ref_status=200, tree_sha="new-tree"):
    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content) if req.content else None
        calls.append((req.method, req.url.path, body))
        p = req.url.path
        if req.method == "GET" and p == "/repos/acme/agent/git/ref/heads/main":
            return httpx.Response(200, json={"object": {"sha": tip}})
        if req.method == "GET" and p == f"/repos/acme/agent/git/commits/{tip}":
            return httpx.Response(200, json={"tree": {"sha": "tip-tree"}})
        if req.method == "POST" and p == "/repos/acme/agent/git/blobs":
            return httpx.Response(201, json={"sha": "blob-sha"})
        if req.method == "POST" and p == "/repos/acme/agent/git/trees":
            return httpx.Response(201, json={"sha": tree_sha})
        if req.method == "POST" and p == "/repos/acme/agent/git/commits":
            return httpx.Response(201, json={"sha": "commit-sha"})
        if req.method == "PATCH" and p == "/repos/acme/agent/git/refs/heads/main":
            return httpx.Response(ref_status, json={"message": "Update is not a fast forward"})
        return httpx.Response(500)

    return handler


def _gh(handler):
    return GitHubRepoService(client=httpx.Client(transport=httpx.MockTransport(handler)))


def _on_head(svc, **kw):
    return svc.commit_files_on_head(
        "acme",
        "agent",
        {"evals/evals.yaml": b"version: 1\n"},
        branch="main",
        expected_head_sha=kw.pop("expected", "head-sha"),
        message="evals: x (via AGP)",
        token="t",
    )


def test_commit_files_on_head_overlays_on_the_tip_tree_and_fast_forwards():
    calls = []
    assert _on_head(_gh(_git_handler(calls))) == "commit-sha"
    tree = next(b for m, p, b in calls if p.endswith("/git/trees"))
    assert tree["base_tree"] == "tip-tree"
    assert tree["tree"] == [{"path": "evals/evals.yaml", "mode": "100644", "type": "blob", "sha": "blob-sha"}]
    commit = next(b for m, p, b in calls if p.endswith("/git/commits") and m == "POST")
    assert commit["parents"] == ["head-sha"] and "[skip ci]" not in commit["message"]
    ref = next(b for m, p, b in calls if m == "PATCH")
    assert ref == {"sha": "commit-sha", "force": False}


def test_commit_files_on_head_refuses_when_the_tip_moved():
    calls = []
    with pytest.raises(GitHubRepoConflictError) as exc:
        _on_head(_gh(_git_handler(calls, tip="someone-else")))
    assert isinstance(exc.value, RepoConflictError) and isinstance(exc.value, GitHubRepoError)
    assert not any(m == "POST" for m, _, _ in calls)


def test_commit_files_on_head_maps_a_non_fast_forward_to_a_conflict():
    with pytest.raises(GitHubRepoConflictError):
        _on_head(_gh(_git_handler([], ref_status=422)))


def test_commit_files_on_head_unchanged_tree_writes_nothing():
    calls = []
    assert _on_head(_gh(_git_handler(calls, tree_sha="tip-tree"))) == "head-sha"
    assert not any(p.endswith("/git/commits") and m == "POST" for m, p, _ in calls)
    assert not any(m == "PATCH" for m, _, _ in calls)


def test_read_file_returns_bytes_at_the_ref():
    seen = []

    def handler(req):
        seen.append((req.url.path, req.url.params.get("ref")))
        return httpx.Response(
            200,
            json={
                "type": "file",
                "encoding": "base64",
                "sha": "s",
                "content": base64.b64encode(b"judges: []\n").decode(),
            },
        )

    assert (
        _gh(handler).read_file("acme", "agent", "evals/evals.yaml", ref="head-sha", token="t")
        == b"judges: []\n"
    )
    assert seen == [("/repos/acme/agent/contents/evals/evals.yaml", "head-sha")]


def test_read_file_404_is_none():
    assert (
        _gh(lambda req: httpx.Response(404, json={"message": "Not Found"})).read_file(
            "acme", "agent", "evals/dataset.jsonl", ref="h", token="t"
        )
        is None
    )


def test_read_file_large_file_falls_back_to_the_blob():
    def handler(req):
        if req.url.path.endswith("/git/blobs/big-sha"):
            return httpx.Response(
                200, json={"encoding": "base64", "content": base64.b64encode(b"x" * 10).decode()}
            )
        return httpx.Response(200, json={"type": "file", "encoding": "none", "content": "", "sha": "big-sha"})

    assert _gh(handler).read_file("acme", "agent", "evals/dataset.jsonl", ref="h", token="t") == b"x" * 10


def test_read_file_other_errors_raise():
    with pytest.raises(GitHubRepoError):
        _gh(lambda req: httpx.Response(403, json={"message": "rate limited"})).read_file(
            "acme", "agent", "evals/evals.yaml", ref="h", token="t"
        )


def _branch_handler(calls, *, target=None, ref_post=201, default="main", race_target=None):
    """``main`` tips at ``src-sha``; ``target`` is the studio branch's current tip (None = absent);
    ``race_target`` is what a re-read of the target answers after a 422 on the POST."""
    state = {"target": target}

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content) if req.content else None
        calls.append((req.method, req.url.path, body))
        p = req.url.path
        if req.method == "GET" and p == "/repos/acme/agent":
            return httpx.Response(200, json={"default_branch": default})
        if req.method == "GET" and p == f"/repos/acme/agent/git/ref/heads/{default}":
            return httpx.Response(200, json={"object": {"sha": "src-sha"}})
        if req.method == "GET" and p == "/repos/acme/agent/git/ref/heads/studio/abcd1234":
            if state["target"] is None:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json={"object": {"sha": state["target"]}})
        if req.method == "GET" and p.startswith("/repos/acme/agent/git/commits/"):
            return httpx.Response(200, json={"tree": {"sha": "some-tree"}})
        if req.method == "POST" and p == "/repos/acme/agent/git/refs":
            if ref_post == 422:
                state["target"] = race_target
                return httpx.Response(422, json={"message": "Reference already exists"})
            assert body is not None
            return httpx.Response(ref_post, json={"object": {"sha": body["sha"]}})
        return httpx.Response(500)

    return handler


def _create(svc, **kw):
    return svc.create_branch("acme", "agent", branch="studio/abcd1234", token="t", **kw)


def test_create_branch_from_the_default_branch_tip():
    calls = []
    assert _create(_gh(_branch_handler(calls, default="trunk")), from_branch=None) == "src-sha"
    post = [b for m, p, b in calls if m == "POST"]
    assert post == [{"ref": "refs/heads/studio/abcd1234", "sha": "src-sha"}]
    assert ("GET", "/repos/acme/agent", None) in calls


def test_create_branch_from_a_named_branch_skips_the_repo_read():
    calls = []
    assert _create(_gh(_branch_handler(calls)), from_branch="main") == "src-sha"
    assert ("GET", "/repos/acme/agent", None) not in calls


def test_create_branch_is_idempotent_on_the_same_sha():
    calls = []
    assert _create(_gh(_branch_handler(calls, target="src-sha")), from_branch="main") == "src-sha"
    assert not any(m == "POST" for m, _, _ in calls)


def test_create_branch_refuses_a_branch_at_another_sha():
    calls = []
    with pytest.raises(GitHubRepoConflictError) as exc:
        _create(_gh(_branch_handler(calls, target="other-sha")), from_branch="main")
    assert isinstance(exc.value, RepoConflictError)
    assert not any(m == "POST" for m, _, _ in calls)


def test_create_branch_race_converges_on_the_same_sha():
    assert (
        _create(_gh(_branch_handler([], ref_post=422, race_target="src-sha")), from_branch="main")
        == "src-sha"
    )


def test_create_branch_race_to_another_sha_is_a_conflict():
    with pytest.raises(GitHubRepoConflictError):
        _create(_gh(_branch_handler([], ref_post=422, race_target="other-sha")), from_branch="main")


def test_create_branch_missing_source_raises():
    def handler(req):
        return httpx.Response(404, json={"message": "Not Found"})

    with pytest.raises(GitHubRepoError):
        _create(_gh(handler), from_branch="nope")


def test_create_branch_other_errors_raise_without_the_token():
    with pytest.raises(GitHubRepoError) as exc:
        _create(_gh(lambda req: httpx.Response(403, json={"message": "nope"})), from_branch="main")
    assert "Bearer" not in str(exc.value)


def test_a_repo_error_renders_the_fixed_rollout_detail():
    err = GitHubRepoError("failed to create blob (HTTP 500: Server Error) ghs_tok")
    assert (err.status_code, err.detail, err.code) == (
        502,
        "Template rollout failed",
        "github_repo.provider_error",
    )
    assert isinstance(GitHubRepoConflictError("branch moved"), GitHubRepoError)
    assert GitHubRepoConflictError("branch moved").status_code == 502
