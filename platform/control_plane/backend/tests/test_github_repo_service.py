"""Direct tests for the GitHub write client (E20/T6) over an httpx.MockTransport.

The rollout/project service tests fake GitHubRepoService wholesale, so the actual
git-data HTTP sequence was never exercised — a real empty repo (auto_init=false) 409s
on the first git/blobs call ("Git Repository is empty"). These tests pin the corrected
sequence: create with auto_init=true, then commit the scaffold onto the auto-init parent
and fast-forward the branch.

E28B/T7 — WHAT THIS FILE NO LONGER TESTS, and why that is not a coverage loss. It used to also
cover ``list_template_repos``, ``generate_from_template`` (+ the whole async-copy readiness wait),
``set_repo_metadata``, ``commit_file``, ``create_environment`` and ``set_environment_variables``.
Those methods are DELETED — they had zero callers once T2/T3/T4 landed — so the tests went with
them rather than being kept as coverage for code that cannot run. The contract that replaced them
is tested in ``test_repo_provider.py`` (the five portable verbs against the same MockTransport
idiom), and ``test_repo_provider.test_the_pre_e28b_github_shaped_methods_are_GONE`` is the fence
that keeps the deleted shapes from coming back.

What is still pinned here: the zip-scaffold rollout path (``create_repo_from_zip``, still used
for template + infra repos) and the delete/probe pair the E23 cascade and delete-preview need.
"""

import io
import zipfile

import httpx
import pytest

from services.github_repo_service import GitHubRepoError, GitHubRepoService


def _zip(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for path, content in files.items():
            zf.writestr(path, content)
    return buf.getvalue()


def test_create_repo_from_zip_auto_inits_then_commits_scaffold():
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append((req.method, req.url.path))
        p = req.url.path
        if req.method == "POST" and p == "/orgs/acme/repos":
            import json
            body = json.loads(req.content)
            # THE fix: repo must be created auto-init'd so the git DB exists.
            assert body["auto_init"] is True
            return httpx.Response(201, json={
                "html_url": "https://github.com/acme/tmpl",
                "owner": {"login": "acme"},
            })
        if req.method == "GET" and p == "/repos/acme/tmpl/git/ref/heads/main":
            return httpx.Response(200, json={"object": {"sha": "parent-sha"}})
        if req.method == "POST" and p == "/repos/acme/tmpl/git/blobs":
            return httpx.Response(201, json={"sha": "blob-sha"})
        if req.method == "POST" and p == "/repos/acme/tmpl/git/trees":
            return httpx.Response(201, json={"sha": "tree-sha"})
        if req.method == "POST" and p == "/repos/acme/tmpl/git/commits":
            import json
            body = json.loads(req.content)
            assert body["parents"] == ["parent-sha"]  # parented onto the auto-init commit
            return httpx.Response(201, json={"sha": "commit-sha"})
        if req.method == "PATCH" and p == "/repos/acme/tmpl/git/refs/heads/main":
            import json
            body = json.loads(req.content)
            assert body["sha"] == "commit-sha" and body["force"] is True
            return httpx.Response(200, json={})
        return httpx.Response(404)

    svc = GitHubRepoService(client=httpx.Client(transport=httpx.MockTransport(handler)))
    url = svc.create_repo_from_zip("acme", "tmpl", _zip({"README.md": "hi", "app/main.py": "x"}), "ghs_tok")

    assert url == "https://github.com/acme/tmpl"
    # Sequence: create repo → read default ref → 2 blobs → tree → commit → patch ref.
    assert ("POST", "/orgs/acme/repos") == calls[0]
    assert ("GET", "/repos/acme/tmpl/git/ref/heads/main") == calls[1]
    assert ("PATCH", "/repos/acme/tmpl/git/refs/heads/main") == calls[-1]
    assert calls.count(("POST", "/repos/acme/tmpl/git/blobs")) == 2


def test_create_repo_from_zip_surfaces_github_message_and_hint():
    # GitHub's own validation message IS surfaced (it's a safe string), plus a re-run hint on 422.
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={
            "message": "Repository creation failed.",
            "errors": [{"message": "name already exists on this account"}],
        })

    svc = GitHubRepoService(client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(GitHubRepoError) as ei:
        svc.create_repo_from_zip("acme", "tmpl", _zip({"f": "x"}), "ghs_tok")
    text = str(ei.value)
    assert "422" in text
    assert "name already exists" in text          # GitHub's message surfaced
    assert "already exist" in text                 # our re-run hint
    assert "ghs_tok" not in text                   # token NEVER leaked


def test_transport_error_is_safe_and_tokenless():
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    svc = GitHubRepoService(client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(GitHubRepoError) as ei:
        svc.create_repo_from_zip("acme", "tmpl", _zip({"f": "x"}), "ghs_tok")
    assert "ghs_tok" not in str(ei.value)


# --------------------------------------------------------------------------- #
# E28B/T7 — a 2xx WITHOUT a `sha` must be a GitHubRepoError, not a bare KeyError.
#
# `_push_initial_commit` read four shas as `resp.json()["sha"]` / `["object"]["sha"]`. On a 2xx
# whose body lacks the key that is a `KeyError`, which ESCAPES this module's contract: every caller
# catches `GitHubRepoError`, and `create_repo_from_zip`'s own except clause catches only
# `httpx.HTTPError`, so a shape-surprising 200 propagated as a type nobody handles. `_require_ok`
# cannot cover it — the status IS 2xx and it is the BODY that is unusable. Same defect T1 closed in
# `commit_files`. Parametrized over EVERY read so a partial repair (which this epic hit twice) fails.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "shaless",
    ["ref", "blob", "tree", "commit"],
)
def test_the_zip_push_surfaces_a_shaless_2xx_as_a_repo_error(shaless):
    def handler(req: httpx.Request) -> httpx.Response:
        p = req.url.path
        if req.method == "POST" and p == "/orgs/acme/repos":
            return httpx.Response(201, json={
                "html_url": "https://github.com/acme/tmpl", "owner": {"login": "acme"},
            })
        if req.method == "GET" and p == "/repos/acme/tmpl/git/ref/heads/main":
            # A 200 with no `object` at all — the shape the chained `.get()` must survive.
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
    assert "ghs_tok" not in text  # token NEVER leaked, even on the surprise path


# --------------------------------------------------------------------------- #
# E22/T1 (delete) + E23/T11 (probe) — the two repo-lifecycle calls that SURVIVED T7.
# --------------------------------------------------------------------------- #

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
    # 204 -> no raise; DELETE hit /repos/acme/r.
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "DELETE" and req.url.path == "/repos/acme/r":
            return httpx.Response(204)
        return httpx.Response(404)

    svc, calls = _svc(handler)
    svc.delete_repo("acme", "r", "tok", base_url=_BASE)
    verbs = [(m, path) for (m, path, _req) in calls]
    assert verbs == [("DELETE", "/repos/acme/r")]


def test_delete_repo_404_is_idempotent():
    # 404 (repo already gone) -> treated as success, no raise.
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    svc, _ = _svc(handler)
    svc.delete_repo("acme", "r", "tok", base_url=_BASE)


def test_delete_repo_error_is_safe():
    # 500 -> GitHubRepoError whose str() contains neither the token nor a raw body.
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={
            "message": "Server Error",
            "documentation_url": "https://docs.github.com/secret-leak",
        })

    svc, _ = _svc(handler)
    with pytest.raises(GitHubRepoError) as ei:
        svc.delete_repo("acme", "r", "ghs_tok", base_url=_BASE)
    text = str(ei.value)
    assert "500" in text
    assert "ghs_tok" not in text                    # token NEVER leaked
    assert "documentation_url" not in text          # no raw body echoed


def test_repo_exists_true_on_200():
    # 200 -> the repo exists (present).
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path == "/repos/acme/r":
            return httpx.Response(200, json={"name": "r"})
        return httpx.Response(404)

    svc, calls = _svc(handler)
    assert svc.repo_exists("acme", "r", "tok", base_url=_BASE) is True
    verbs = [(m, path) for (m, path, _req) in calls]
    assert verbs == [("GET", "/repos/acme/r")]


def test_repo_exists_false_on_404():
    # 404 -> the repo is gone.
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    svc, _ = _svc(handler)
    assert svc.repo_exists("acme", "r", "tok", base_url=_BASE) is False


def test_repo_exists_error_is_safe():
    # 500 -> GitHubRepoError whose str() carries neither the token nor a raw body.
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={
            "message": "Server Error",
            "documentation_url": "https://docs.github.com/secret-leak",
        })

    svc, _ = _svc(handler)
    with pytest.raises(GitHubRepoError) as ei:
        svc.repo_exists("acme", "r", "ghs_tok", base_url=_BASE)
    text = str(ei.value)
    assert "500" in text
    assert "ghs_tok" not in text
    assert "documentation_url" not in text

def test_actions_review_methods_removed():
    """E27/T10 — the GitHub-Actions-only deployment-review surface is DELETED, not shimmed.

    `list_waiting_runs` / `list_pending_deployments` / `review_deployment` wrapped GitHub's
    `pending_deployments` review API, which has no GitLab equivalent. Prod promotion is now an
    AGP action (OWNER-gated `POST /projects/{id}/repos/{repo_id}/promote`), so re-adding any of
    these would reintroduce a provider-specific approval path AGP no longer needs.
    """
    for gone in ("list_waiting_runs", "list_pending_deployments", "review_deployment"):
        assert not hasattr(GitHubRepoService, gone)

