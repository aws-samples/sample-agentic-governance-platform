"""Pull-request verbs on a repository, in two layers.

Layer 1 is the service over an ``httpx.MockTransport``: which endpoint each verb calls, how a
GitHub PR object is projected onto :class:`~models.repository.PullRequestView`, and which failures
become which kind. Layer 2 is the routes on ``repositories_router`` against a mocked
``verify_entra_token`` with the services as ``MagicMock`` module globals: the gate, the
404-not-403 contract, and the invariant that the only credential reaching the service is the linked
human's token. No boto3, no live AWS, no live GitHub.

The load-bearing invariants: no App-token fallback on any path (the service module cannot even
reach one, and the route refuses rather than retries); a self-approval is refused with a reason
before GitHub is called; a missing ``pull_requests`` grant is a hidden tab, never a 500; a merge
failure surfaces a fixed hint only; ``mergeable`` is trusted only as ``False``.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from models.project import Project, ProjectDetail
from models.repository import Repository
from services.github_pr_service import GitHubPrDuplicateError, GitHubPrError, GitHubPrService

pytestmark = pytest.mark.usefixtures("reset_modules")

FIXED_TS = "2026-07-31T00:00:00+00:00"
VIEWER_LOGIN = "lars-svensson"
ACCOUNT_SHAPED = "111122223333"


def _pr_payload(**over) -> dict:
    """A GitHub PR object trimmed to the keys the projection reads. ``mergeable`` is absent
    because the list endpoint omits it; tests modelling a single-PR body pass it explicitly."""
    payload = {
        "number": 7,
        "title": "Raise the claim-triage threshold",
        "state": "open",
        "user": {"login": "jorge"},
        "head": {"sha": "3f9a1c2b4d5e6f7a8b9c", "ref": "feature/threshold"},
        "html_url": "https://github.com/acme/claims-triage/pull/7",
        "merged_at": None,
    }
    payload.update(over)
    return payload


def _svc(handler) -> GitHubPrService:
    return GitHubPrService(client=httpx.Client(transport=httpx.MockTransport(handler)))


def _ok(payload):
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    return handler


def test_list_projects_the_provider_object_onto_the_view():
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append((req.method, req.url.path, dict(req.url.params)))
        return httpx.Response(200, json=[_pr_payload()])

    rows = _svc(handler).list_pull_requests(
        "acme", "claims-triage", "ghu_user_token", viewer_login=VIEWER_LOGIN
    )

    assert calls == [("GET", "/repos/acme/claims-triage/pulls", {"state": "all", "per_page": "30"})]
    assert len(rows) == 1
    row = rows[0]
    assert row.number == 7
    assert row.title == "Raise the claim-triage threshold"
    assert row.state == "open"
    assert row.author == "jorge"
    assert row.head_sha == "3f9a1c2b4d5e6f7a8b9c"
    assert row.url == "https://github.com/acme/claims-triage/pull/7"
    assert row.mergeable is None


def test_the_user_token_is_the_only_authorization_sent():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["auth"] = req.headers.get("authorization")
        seen["accept"] = req.headers.get("accept")
        return httpx.Response(200, json=[])

    _svc(handler).list_pull_requests("acme", "r", "ghu_user_token", viewer_login=VIEWER_LOGIN)

    assert seen["auth"] == "Bearer ghu_user_token"
    assert seen["accept"] == "application/vnd.github+json"


def test_a_merged_pr_reports_merged_not_closed():
    rows = _svc(_ok([_pr_payload(state="closed", merged_at="2026-07-31T09:00:00Z")])).list_pull_requests(
        "acme", "r", "t", viewer_login=VIEWER_LOGIN
    )

    assert rows[0].state == "merged"


def test_merge_commit_sha_only_once_merged():
    """GitHub fills ``merge_commit_sha`` on an open PR too (its test merge commit), which no build
    ever runs, so it is reported only once ``merged_at`` is set."""
    merged, open_ = _svc(
        _ok(
            [
                _pr_payload(state="closed", merged_at="2026-07-31T09:00:00Z", merge_commit_sha="abc123"),
                _pr_payload(number=8, merge_commit_sha="testmerge999"),
            ]
        )
    ).list_pull_requests("acme", "r", "t", viewer_login=VIEWER_LOGIN)

    assert merged.merge_commit_sha == "abc123"
    assert open_.merge_commit_sha is None


def test_a_genuinely_closed_pr_stays_closed():
    rows = _svc(_ok([_pr_payload(state="closed", merged_at=None)])).list_pull_requests(
        "acme", "r", "t", viewer_login=VIEWER_LOGIN
    )

    assert rows[0].state == "closed"


@pytest.mark.parametrize("payload", [{}, {"mergeable": None}], ids=["absent", "explicit-null"])
def test_an_absent_and_an_explicit_null_mergeable_both_project_to_none(payload):
    """The two are indistinguishable after projection on purpose, so no surface can claim "the
    provider is still working it out"; what holds is that neither is ``False``."""
    rows = _svc(_ok([_pr_payload(**payload)])).list_pull_requests("acme", "r", "t", viewer_login=VIEWER_LOGIN)

    assert rows[0].mergeable is None
    assert rows[0].mergeable is not False


def test_a_single_pr_read_still_carries_a_real_mergeable():
    view = _svc(_ok(_pr_payload(mergeable=True))).create_pull_request(
        "acme", "r", "t", viewer_login=VIEWER_LOGIN, title="t", head="h", base=None, body=None
    )

    assert view.mergeable is True

    unmergeable = _svc(_ok(_pr_payload(mergeable=False))).create_pull_request(
        "acme", "r", "t", viewer_login=VIEWER_LOGIN, title="t", head="h", base=None, body=None
    )

    assert unmergeable.mergeable is False


def test_a_pr_with_no_author_object_reads_as_absent_never_as_the_viewer():
    """A deleted GitHub account leaves ``user: null``; with no author there is nobody to be, so
    nothing establishes a self-approval."""
    rows = _svc(_ok([_pr_payload(user=None)])).list_pull_requests("acme", "r", "t", viewer_login=VIEWER_LOGIN)

    assert rows[0].author == ""
    assert rows[0].can_approve is True


def test_a_malformed_row_is_skipped_not_crashed():
    rows = _svc(_ok(["nonsense", _pr_payload(), {"no": "number"}])).list_pull_requests(
        "acme", "r", "t", viewer_login=VIEWER_LOGIN
    )

    assert [r.number for r in rows] == [7]


def test_a_non_list_body_is_empty_not_an_exception():
    rows = _svc(_ok({"message": "surprise"})).list_pull_requests("acme", "r", "t", viewer_login=VIEWER_LOGIN)

    assert rows == []


def test_can_approve_is_false_when_the_linked_human_is_the_author():
    rows = _svc(_ok([_pr_payload(user={"login": VIEWER_LOGIN})])).list_pull_requests(
        "acme", "r", "t", viewer_login=VIEWER_LOGIN
    )

    assert rows[0].can_approve is False
    assert rows[0].approve_blocked_reason
    assert "own" in rows[0].approve_blocked_reason.lower()


def test_the_author_comparison_is_case_insensitive():
    rows = _svc(_ok([_pr_payload(user={"login": VIEWER_LOGIN.upper()})])).list_pull_requests(
        "acme", "r", "t", viewer_login=VIEWER_LOGIN
    )

    assert rows[0].can_approve is False


def test_a_closed_or_merged_pr_cannot_be_approved_either():
    """Its own reason, and one that does not read as the self-approval one."""
    for over in [{"state": "closed"}, {"state": "closed", "merged_at": "2026-07-31T09:00:00Z"}]:
        rows = _svc(_ok([_pr_payload(**over)])).list_pull_requests(
            "acme", "r", "t", viewer_login=VIEWER_LOGIN
        )
        assert rows[0].can_approve is False, over
        assert rows[0].approve_blocked_reason
        assert "own" not in rows[0].approve_blocked_reason.lower(), over


def test_an_unknown_viewer_login_refuses_rather_than_permits():
    rows = _svc(_ok([_pr_payload()])).list_pull_requests("acme", "r", "t", viewer_login="")

    assert rows[0].can_approve is False
    assert rows[0].approve_blocked_reason


def test_approve_refuses_a_self_approval_without_calling_the_review_endpoint():
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append((req.method, req.url.path))
        return httpx.Response(200, json=_pr_payload(user={"login": VIEWER_LOGIN}))

    with pytest.raises(GitHubPrError) as ei:
        _svc(handler).approve_pull_request(
            "acme", "claims-triage", 7, "ghu_user_token", viewer_login=VIEWER_LOGIN
        )

    assert ei.value.kind == "self_approval"
    assert calls == [("GET", "/repos/acme/claims-triage/pulls/7")]


def test_approve_refuses_a_closed_pull_request_as_not_approvable():
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append((req.method, req.url.path))
        return httpx.Response(200, json=_pr_payload(state="closed"))

    with pytest.raises(GitHubPrError) as ei:
        _svc(handler).approve_pull_request(
            "acme", "claims-triage", 7, "ghu_user_token", viewer_login=VIEWER_LOGIN
        )

    assert ei.value.kind == "not_approvable"
    assert calls == [("GET", "/repos/acme/claims-triage/pulls/7")]


def test_approve_posts_a_review_for_someone_elses_pr():
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append((req.method, req.url.path))
        if req.method == "POST":
            assert json.loads(req.content) == {"event": "APPROVE"}
            return httpx.Response(200, json={"id": 1, "state": "APPROVED"})
        return httpx.Response(200, json=_pr_payload())

    view = _svc(handler).approve_pull_request(
        "acme", "claims-triage", 7, "ghu_user_token", viewer_login=VIEWER_LOGIN
    )

    assert ("POST", "/repos/acme/claims-triage/pulls/7/reviews") in calls
    assert calls[-1] == ("GET", "/repos/acme/claims-triage/pulls/7")
    assert view.number == 7


def test_approve_maps_the_providers_own_self_review_refusal_too():
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            return httpx.Response(422, json={"message": "Can not approve your own pull request"})
        return httpx.Response(200, json=_pr_payload())

    with pytest.raises(GitHubPrError) as ei:
        _svc(handler).approve_pull_request("acme", "r", 7, "t", viewer_login="someone-else")

    assert ei.value.kind == "self_approval"


def test_create_posts_the_title_head_and_base():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            seen["path"] = req.url.path
            seen["body"] = json.loads(req.content)
            return httpx.Response(201, json=_pr_payload())
        return httpx.Response(404)

    view = _svc(handler).create_pull_request(
        "acme",
        "claims-triage",
        "ghu_user_token",
        viewer_login=VIEWER_LOGIN,
        title="Raise the threshold",
        head="feature/threshold",
        base="release-line",
        body="why",
    )

    assert seen["path"] == "/repos/acme/claims-triage/pulls"
    assert seen["body"] == {
        "title": "Raise the threshold",
        "head": "feature/threshold",
        "base": "release-line",
        "body": "why",
    }
    assert view.number == 7


def test_create_omits_base_entirely_when_the_caller_named_none():
    """An omitted base means the repository's own default branch, resolved by the provider; an
    absent body is absent, not an empty comment."""
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(req.content)
        return httpx.Response(201, json=_pr_payload())

    _svc(handler).create_pull_request(
        "acme", "r", "t", viewer_login=VIEWER_LOGIN, title="t", head="h", base=None, body=None
    )

    assert "base" not in seen["body"]
    assert "body" not in seen["body"]


def test_create_maps_a_duplicate_pr_to_conflict():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422, json={"errors": [{"message": "A pull request already exists for acme:x."}]}
        )

    with pytest.raises(GitHubPrError) as ei:
        _svc(handler).create_pull_request(
            "acme", "r", "t", viewer_login=VIEWER_LOGIN, title="t", head="h", base=None, body=None
        )

    assert ei.value.kind == "conflict"


def test_create_maps_no_commits_between_the_branches_to_its_own_kind():
    """GitHub states it in ``errors[].message``, not ``message``; the classification crosses and
    the body text does not."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={
                "message": "Validation Failed",
                "errors": [{"message": "No commits between main and dev"}],
            },
        )

    with pytest.raises(GitHubPrError) as ei:
        _svc(handler).create_pull_request(
            "acme", "r", "t", viewer_login=VIEWER_LOGIN, title="t", head="h", base=None, body=None
        )

    assert ei.value.kind == "no_commits"
    assert "main" not in str(ei.value)
    assert "dev" not in str(ei.value)


def test_merge_puts_to_the_merge_endpoint_and_returns_the_fresh_view():
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append((req.method, req.url.path))
        if req.method == "PUT":
            return httpx.Response(200, json={"merged": True, "sha": "abc"})
        return httpx.Response(200, json=_pr_payload(state="closed", merged_at="2026-07-31T09:00:00Z"))

    view = _svc(handler).merge_pull_request("acme", "claims-triage", 7, "t", viewer_login=VIEWER_LOGIN)

    assert ("PUT", "/repos/acme/claims-triage/pulls/7/merge") in calls
    assert calls[-1] == ("GET", "/repos/acme/claims-triage/pulls/7")
    assert view.state == "merged"


@pytest.mark.parametrize(
    "status,kind",
    [(405, "not_mergeable"), (409, "conflict"), (403, "capability_missing"), (404, "not_found")],
)
def test_merge_maps_each_provider_refusal_to_its_own_kind(status, kind):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "PUT":
            return httpx.Response(status, json={"message": "provider prose"})
        return httpx.Response(200, json=_pr_payload())

    with pytest.raises(GitHubPrError) as ei:
        _svc(handler).merge_pull_request("acme", "r", 7, "t", viewer_login=VIEWER_LOGIN)

    assert ei.value.kind == kind


def test_a_merge_failure_never_carries_the_provider_body():
    secret_ish = f"Head branch was modified by {ACCOUNT_SHAPED}/acme"

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "PUT":
            return httpx.Response(409, json={"message": secret_ish})
        return httpx.Response(200, json=_pr_payload())

    with pytest.raises(GitHubPrError) as ei:
        _svc(handler).merge_pull_request("acme", "r", 7, "t", viewer_login=VIEWER_LOGIN)

    text = str(ei.value)
    assert secret_ish not in text
    assert ACCOUNT_SHAPED not in text
    assert len(text) > 10


def test_a_403_on_the_list_is_the_missing_grant_signal():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "Resource not accessible by integration"})

    with pytest.raises(GitHubPrError) as ei:
        _svc(handler).list_pull_requests("acme", "r", "t", viewer_login=VIEWER_LOGIN)

    assert ei.value.kind == "capability_missing"


def test_a_404_on_the_list_is_not_found_not_a_missing_grant():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    with pytest.raises(GitHubPrError) as ei:
        _svc(handler).list_pull_requests("acme", "r", "t", viewer_login=VIEWER_LOGIN)

    assert ei.value.kind == "not_found"


def test_a_transport_failure_is_a_provider_error_and_names_no_url():
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("failed to connect to https://x?token=ghu_leak")

    with pytest.raises(GitHubPrError) as ei:
        _svc(handler).list_pull_requests("acme", "r", "t", viewer_login=VIEWER_LOGIN)

    assert ei.value.kind == "provider_error"
    assert "ghu_leak" not in str(ei.value)
    assert "ConnectError" in str(ei.value)


def test_no_error_message_anywhere_carries_the_token():
    token = "ghu_super_secret_value"

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"message": "boom"})

    svc = GitHubPrService(client=httpx.Client(transport=httpx.MockTransport(handler)))
    attempts = [
        lambda: svc.list_pull_requests("acme", "r", token, viewer_login=VIEWER_LOGIN),
        lambda: svc.create_pull_request(
            "acme", "r", token, viewer_login=VIEWER_LOGIN, title="t", head="h", base=None, body=None
        ),
        lambda: svc.approve_pull_request("acme", "r", 7, token, viewer_login=VIEWER_LOGIN),
        lambda: svc.merge_pull_request("acme", "r", 7, token, viewer_login=VIEWER_LOGIN),
    ]
    for attempt in attempts:
        with pytest.raises(GitHubPrError) as ei:
            attempt()
        assert token not in str(ei.value)
        assert "ghu_" not in str(ei.value)


def test_the_service_module_cannot_reach_an_app_token():
    """Asserted over the raw source, because an import added later is exactly the change a
    reviewer would otherwise have to catch by eye."""
    src = Path("src/services/github_pr_service.py").read_text()
    assert len(src) > 500, "read the real module, not an empty file"
    for forbidden in [
        "connection_service",
        "ConnectionService",
        "get_bearer_token",
        "github_app_auth",
        "mint_installation_token",
        "build_app_jwt",
        "installation",
        "boto3",
    ]:
        assert forbidden not in src, f"github_pr_service reaches an App-token seam: {forbidden}"


@pytest.mark.parametrize(
    "kind,status,detail",
    [
        ("capability_missing", 403, "pull requests are not enabled for this organization"),
        ("not_found", 404, "pull request not found"),
        ("self_approval", 409, "you cannot approve your own pull request"),
        ("not_approvable", 409, "this pull request cannot be approved"),
        ("not_mergeable", 409, "this pull request cannot be merged yet"),
        ("conflict", 409, "GitHub declined the request for this pull request"),
        ("no_commits", 409, "there are no commits between these branches"),
        ("provider_error", 502, "GitHub request failed"),
    ],
)
def test_each_kind_renders_its_fixed_detail(kind, status, detail):
    err = GitHubPrError(f"merge conflict in acme/claims-triage at {ACCOUNT_SHAPED}", kind=kind)
    assert (err.status_code, err.detail, err.code) == (status, detail, f"github_pr.{kind}")
    assert ACCOUNT_SHAPED not in str(err.detail)


def _kinds_declared_in(source: str) -> set[str]:
    """Every ``kind`` value the module source can produce: an explicit ``kind="…"`` argument at
    a call site, or the ``kind`` parameter default on ``GitHubPrError.__init__``. Read from the
    parse tree, so prose mentioning a kind contributes nothing."""
    tree = ast.parse(source)
    kinds = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "kind":
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                kinds.add(node.value.value)
        elif isinstance(node, ast.arguments):
            named = [*node.posonlyargs, *node.args, *node.kwonlyargs]
            padding = [None] * (len(node.posonlyargs) + len(node.args) - len(node.defaults))
            defaults = [*padding, *node.defaults, *node.kw_defaults]
            for arg, default in zip(named, defaults, strict=True):
                if arg.arg == "kind" and isinstance(default, ast.Constant) and isinstance(default.value, str):
                    kinds.add(default.value)
    return kinds


def test_every_kind_the_service_can_produce_is_a_table_row_and_vice_versa():
    """Compared mechanically in both directions: a new kind with no row would be a ``TypeError``
    at the raise site, and a row no kind produces is dead copy that reads as live."""
    source = Path("src/services/github_pr_service.py").read_text()
    assert len(source) > 500, "read the real module, not an empty file"

    produced = _kinds_declared_in(source)
    mapped = set(GitHubPrError.kinds)

    assert produced, "the extractor found no kinds at all — it has stopped seeing the module"
    assert produced - mapped == set(), f"service kinds with no table row: {produced - mapped}"
    assert mapped - produced == set(), f"table rows no service kind produces: {mapped - produced}"


def test_that_guard_cannot_be_satisfied_by_PROSE_mentioning_a_kind():
    prose_only = '''
"""A docstring naming kind="ghost_from_the_docstring" and ``kind`` generally."""
# A comment naming kind="ghost_from_the_comment".
value = "kind=ghost_in_a_plain_string"
'''
    assert _kinds_declared_in(prose_only) == set()

    assert _kinds_declared_in('raise GitHubPrError("x", kind="real_at_a_call_site")') == {
        "real_at_a_call_site"
    }
    assert _kinds_declared_in('def __init__(self, m, kind: str = "real_as_a_default"): pass') == {
        "real_as_a_default"
    }
    assert _kinds_declared_in('reason = "not_a_kind_at_all"') == set()


def test_the_kind_is_keyword_only_and_checked_against_the_table_at_construction():
    """A positional kind cannot be written at all, and a kind outside the table fails at the raise
    site rather than shipping as an unmapped 502."""
    untyped: Any = GitHubPrError
    with pytest.raises(TypeError):
        untyped("x", "conflict")
    with pytest.raises(TypeError, match="new_kind"):
        GitHubPrError("x", kind="new_kind")

    assert GitHubPrError("x", kind="conflict").kind == "conflict"
    assert GitHubPrError("x").kind == "provider_error"


def test_a_duplicate_pr_422_is_its_own_conflict_subclass():
    """Studio tells "a PR already exists" from every other 422; the kind is unchanged."""

    def dup(req):
        return httpx.Response(
            422,
            json={
                "message": "Validation Failed",
                "errors": [{"message": "A pull request already exists for acme:studio/x."}],
            },
        )

    with pytest.raises(GitHubPrDuplicateError) as exc:
        _svc(dup).create_pull_request(
            "acme", "r", "t", viewer_login=VIEWER_LOGIN, title="t", head="studio/x", base=None, body=None
        )
    assert exc.value.kind == "conflict"

    def other(req):
        return httpx.Response(
            422, json={"message": "Validation Failed", "errors": [{"message": "head invalid"}]}
        )

    with pytest.raises(GitHubPrError) as exc:
        _svc(other).create_pull_request(
            "acme", "r", "t", viewer_login=VIEWER_LOGIN, title="t", head="studio/x", base=None, body=None
        )
    assert exc.value.kind == "conflict" and not isinstance(exc.value, GitHubPrDuplicateError)


def _project(id="prj-1", tenant_id="default"):
    return Project(
        id=id,
        name="Fraud Ops",
        connection_id="conn-1",
        tenant_id=tenant_id,
        description="",
        created_by="operator@x.com",
        created_at=FIXED_TS,
        updated_at=FIXED_TS,
    )


def _repository(id="repo-1", project_id="prj-1"):
    return Repository(
        id=id,
        project_id=project_id,
        name="claims-triage",
        repo_url="https://github.com/acme/claims-triage",
        agent_id="a-1",
        template_name="strands-agentcore",
        cicd_status="deployed",
        status="ready",
        created_by="operator@x.com",
        created_at=FIXED_TS,
        updated_at=FIXED_TS,
    )


def _tenant_context(*, is_global=False, tenant_ids=("default",)):
    from services.tenant_resolver import TenantContext

    return TenantContext(is_global=is_global, tenant_ids=frozenset(tenant_ids), tenants=())


def _project_context(*, is_global=False, role=None, project_id="prj-1"):
    from models.project_role import ROLE_NAMES
    from services.project_resolver import ProjectContext

    roles = {} if role is None else {project_id: ROLE_NAMES[role]}
    return ProjectContext(is_global=is_global, roles=roles)


class _FakeTenantResolver:
    def __init__(self, ctx):
        self._ctx = ctx

    async def resolve(self, principal):
        return self._ctx


class _FakeProjectResolver:
    def __init__(self, ctx):
        self._ctx = ctx
        self.invalidate = MagicMock()

    async def resolve(self, principal):
        return self._ctx

    def refresh_project(self, principal, project_id):
        return self._ctx


def _claims():
    return {
        "oid": "lars-oid",
        "preferred_username": "lars.svensson@example.com",
        "roles": ["Platform.Operator"],
    }


def _link(login=VIEWER_LOGIN):
    from models.github_link import GitHubUserLink, LinkStatus

    return GitHubUserLink(
        id="lnk-1",
        principal_oid="lars-oid",
        connection_id="conn-1",
        github_id=4242,
        github_login=login,
        status=LinkStatus.LINKED,
        secret_arn="arn:aws:secretsmanager:eu-central-1:000000000000:secret:agp-dev/lnk-1",
        created_at=FIXED_TS,
        updated_at=FIXED_TS,
    )


@pytest.fixture
def wired(entra_settings):
    """The four collaborators the PR routes compose, as MagicMocks on the module globals, on the
    happy path: a visible project, a repo under it, a live GitHub link and a PR service that
    answers. Each test perturbs exactly one."""
    import api.routes.projects as projects_module

    project_svc = MagicMock()
    project_svc.get_repo.return_value = _repository()
    project_svc.get_project.return_value = ProjectDetail(project=_project(), repositories=[])
    projects_module._svc = project_svc

    role_svc = MagicMock()
    role_svc.has_role_rows.return_value = False
    role_svc.list_all_strict.return_value = []
    projects_module._role_svc = role_svc

    connection_svc = MagicMock()
    connection_svc.get_connection.return_value = MagicMock(org="acme", base_url=None)

    link_svc = MagicMock()
    link_svc.get_user_bearer_token.return_value = "ghu_user_token"
    link_svc.list_for_principal.return_value = [_link()]

    pr_svc = MagicMock()
    pr_svc.list_pull_requests.return_value = []

    return {
        "project": project_svc,
        "role": role_svc,
        "connection": connection_svc,
        "link": link_svc,
        "pr": pr_svc,
    }


def _client(wired, *, project_role: str | None = "maintainer", is_global=False):
    import api.routes.repositories as repositories_module
    import api.routes.users as users_module
    from core.errors import install_error_handlers

    users_module._tenant_resolver = _FakeTenantResolver(_tenant_context())
    users_module._project_resolver = _FakeProjectResolver(
        _project_context(is_global=is_global, role=project_role)
    )
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(repositories_module.repositories_router, prefix="/api/v1")
    return TestClient(app, headers={"Authorization": "Bearer fake-token"})


def _call(wired, method, path, body=None, *, project_role: str | None = "maintainer", is_global=False):
    client = _client(wired, project_role=project_role, is_global=is_global)
    with (
        patch("core.security_entra.verify_entra_token", return_value=_claims()),
        patch("api.routes.connections.get_connection_service", return_value=wired["connection"]),
        patch("api.routes.github_link.get_github_link_service", return_value=wired["link"]),
        patch("api.routes.repositories.get_pr_service", return_value=wired["pr"]),
    ):
        return getattr(client, method)(path, **({"json": body} if body is not None else {}))


LIST_URL = "/api/v1/repositories/repo-1/pull-requests"
APPROVE_URL = "/api/v1/repositories/repo-1/pull-requests/7/approve"
MERGE_URL = "/api/v1/repositories/repo-1/pull-requests/7/merge"
CREATE_BODY = {"title": "t", "head": "feature/x"}


def _view(**over):
    from models.repository import PullRequestView

    payload: dict[str, Any] = {
        "number": 7,
        "title": "Raise the threshold",
        "state": "open",
        "author": "jorge",
        "head_sha": "3f9a1c2",
        "url": "https://github.com/acme/claims-triage/pull/7",
        "can_approve": True,
    }
    payload.update(over)
    return PullRequestView(**payload)


def test_the_four_routes_are_mounted_at_the_pinned_paths():
    """These four paths are what the frontend's ``pullRequestsApi`` calls; a drift here 404s the
    frontend and nothing else notices."""
    import api.routes.repositories as repositories_module

    mounted = {
        (m, r.path)
        for r in repositories_module.repositories_router.routes
        if isinstance(r, APIRoute)
        for m in getattr(r, "methods", set())
    }
    assert ("GET", "/repositories/{repo_id}/pull-requests") in mounted
    assert ("POST", "/repositories/{repo_id}/pull-requests") in mounted
    assert ("POST", "/repositories/{repo_id}/pull-requests/{number}/approve") in mounted
    assert ("POST", "/repositories/{repo_id}/pull-requests/{number}/merge") in mounted


def test_create_answers_201():
    import api.routes.repositories as repositories_module

    route = next(
        r
        for r in repositories_module.repositories_router.routes
        if getattr(r, "path", "") == "/repositories/{repo_id}/pull-requests"
        and "POST" in getattr(r, "methods", set())
    )
    assert isinstance(route, APIRoute)
    assert route.status_code == 201


def test_the_four_pr_routes_carry_a_gate_identical_to_the_routers_existing_read():
    """Two reads of one resource must not carry different guards, because whichever is looser is
    the gate; compared mechanically against ``GET /repositories``."""
    import api.routes.repositories as repositories_module
    from core.rbac import Role

    def deps(path, method):
        route = next(
            r
            for r in repositories_module.repositories_router.routes
            if getattr(r, "path", "") == path and method in getattr(r, "methods", set())
        )
        assert isinstance(route, APIRoute)
        out = []
        for d in route.dependant.dependencies:
            assert d.call is not None
            captured = tuple(c.cell_contents for c in (d.call.__closure__ or ()))
            out.append((d.name, d.call.__qualname__, captured))
        return out

    existing = deps("/repositories", "GET")
    assert len(existing) == 4
    assert [d[0] for d in existing] == ["principal", "ctx", "pctx", "_"]
    assert existing[3][2] == (Role.OPERATOR,)

    for path, method in [
        ("/repositories/{repo_id}/pull-requests", "GET"),
        ("/repositories/{repo_id}/pull-requests", "POST"),
        ("/repositories/{repo_id}/pull-requests/{number}/approve", "POST"),
        ("/repositories/{repo_id}/pull-requests/{number}/merge", "POST"),
    ]:
        assert deps(path, method) == existing, f"{method} {path} carries a different gate"


def test_a_foreign_tenants_repo_is_byte_identically_absent(wired):
    """A repo in another tenant looks absent, identically to a missing id, and no credential is
    resolved and no provider call happens before the gate has run."""
    wired["project"].get_project.return_value = ProjectDetail(
        project=_project(tenant_id="ten-other"), repositories=[]
    )
    foreign = _call(wired, "get", LIST_URL)

    wired["project"].get_repo.return_value = None
    missing = _call(wired, "get", "/api/v1/repositories/nope/pull-requests")

    assert foreign.status_code == 404
    assert missing.status_code == 404
    assert foreign.json() == missing.json()
    assert foreign.json()["detail"] == "Repository not found"
    wired["pr"].list_pull_requests.assert_not_called()
    wired["link"].get_user_bearer_token.assert_not_called()


def test_a_repo_whose_project_vanished_is_absent_not_a_500(wired):
    wired["project"].get_project.return_value = None

    resp = _call(wired, "get", LIST_URL)

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Repository not found"


def test_reading_the_list_needs_viewer_and_writing_needs_maintainer(wired):
    """The same threshold ``retry`` carries, not promote's strict OWNER: a merge registers a prod
    candidate that still needs an OWNER's promote."""
    wired["pr"].list_pull_requests.return_value = [_view()]
    wired["pr"].create_pull_request.return_value = _view()
    wired["role"].has_role_rows.return_value = True

    assert _call(wired, "get", LIST_URL, project_role="viewer").status_code == 200
    for method, url, body in [
        ("post", LIST_URL, CREATE_BODY),
        ("post", APPROVE_URL, None),
        ("post", MERGE_URL, None),
    ]:
        resp = _call(wired, method, url, body, project_role="viewer")
        assert resp.status_code == 403, url
        assert resp.json()["detail"] == "insufficient project role"


def test_a_viewer_cannot_reach_the_provider_at_all(wired):
    wired["role"].has_role_rows.return_value = True

    _call(wired, "post", LIST_URL, CREATE_BODY, project_role="viewer")

    wired["pr"].create_pull_request.assert_not_called()
    wired["link"].get_user_bearer_token.assert_not_called()


def test_a_platform_admin_needs_no_project_role(wired):
    wired["pr"].list_pull_requests.return_value = [_view()]

    resp = _call(wired, "get", LIST_URL, project_role=None, is_global=True)

    assert resp.status_code == 200


def test_the_only_credential_that_reaches_the_service_is_the_linked_users(wired):
    wired["pr"].list_pull_requests.return_value = [_view()]

    assert _call(wired, "get", LIST_URL).status_code == 200

    wired["link"].get_user_bearer_token.assert_called_once_with("lars-oid", "conn-1")
    _org, _repo, token = wired["pr"].list_pull_requests.call_args.args
    assert token == "ghu_user_token"
    wired["connection"].get_bearer_token.assert_not_called()


def test_a_missing_link_refuses_and_does_NOT_retry_under_an_app_token(wired):
    """A human with no GitHub link cannot act as themselves, so AGP does nothing rather than
    acting as itself: the route reads the caller's link rows, finds none for this connection and
    refuses before any token is resolved."""
    wired["link"].list_for_principal.return_value = []

    resp = _call(wired, "get", LIST_URL)

    assert resp.status_code == 409
    assert "GitHub account" in resp.json()["detail"]
    wired["link"].get_user_bearer_token.assert_not_called()
    wired["connection"].get_bearer_token.assert_not_called()
    wired["pr"].list_pull_requests.assert_not_called()


def test_a_revoked_link_refuses_the_same_way(wired):
    from models.github_link import LinkStatus

    revoked = _link()
    revoked.status = LinkStatus.UNLINKED
    wired["link"].list_for_principal.return_value = [revoked]

    resp = _call(wired, "get", LIST_URL)

    assert resp.status_code == 409
    assert "GitHub account" in resp.json()["detail"]
    wired["link"].get_user_bearer_token.assert_not_called()
    wired["connection"].get_bearer_token.assert_not_called()


def test_the_viewer_login_comes_from_the_link_row_not_from_the_caller(wired):
    """``can_approve`` is decided against the GitHub login on the link row, a provider currency,
    never derived from the Entra principal."""
    wired["pr"].list_pull_requests.return_value = [_view()]

    _call(wired, "get", LIST_URL)

    assert wired["pr"].list_pull_requests.call_args.kwargs["viewer_login"] == VIEWER_LOGIN


def test_a_link_on_another_org_is_not_used_for_this_one(wired):
    """A link to another org is not a link for this connection: the caller is refused as unlinked and
    the other org's login never becomes the viewer."""
    other = _link(login="someone-else")
    other.connection_id = "conn-other"
    wired["link"].list_for_principal.return_value = [other]
    wired["pr"].list_pull_requests.return_value = [_view()]

    resp = _call(wired, "get", LIST_URL)

    assert resp.status_code == 409
    wired["link"].get_user_bearer_token.assert_not_called()
    wired["pr"].list_pull_requests.assert_not_called()


def test_a_vanished_connection_is_the_tabs_502_not_the_connection_tables_404(wired):
    """The project's connection is gone, so AGP cannot address the provider: the service's
    ``connection_unavailable`` row, the same 502 "GitHub request failed" the tab keys on, never the
    connection domain's 404; no token is resolved and GitHub is never reached."""
    from services.connection_service import ConnectionError

    wired["connection"].get_connection.side_effect = ConnectionError("gone", kind="not_found")

    resp = _call(wired, "get", LIST_URL)

    assert resp.status_code == 502
    assert resp.json() == {"detail": "GitHub request failed", "code": "github_pr.connection_unavailable"}
    wired["link"].get_user_bearer_token.assert_not_called()
    wired["pr"].list_pull_requests.assert_not_called()


def test_a_missing_org_grant_is_a_mapped_403_never_a_500(wired):
    wired["pr"].list_pull_requests.side_effect = GitHubPrError("nope", kind="capability_missing")

    resp = _call(wired, "get", LIST_URL)

    assert resp.status_code == 403
    assert resp.json()["detail"] == "pull requests are not enabled for this organization"


def test_every_service_kind_is_mapped_so_none_reaches_an_unhandled_500(wired):
    """``provider_error`` is deliberately the one 5xx (a real GitHub outage is not a missing
    grant); the capability refusal is never 5xx."""
    expected = {
        "capability_missing": 403,
        "not_found": 404,
        "conflict": 409,
        "no_commits": 409,
        "self_approval": 409,
        "not_approvable": 409,
        "not_mergeable": 409,
        "provider_error": 502,
    }
    for kind, status in expected.items():
        wired["pr"].list_pull_requests.side_effect = GitHubPrError("x", kind=kind)
        resp = _call(wired, "get", LIST_URL)
        assert resp.status_code == status, f"{kind} answered {resp.status_code}"
        assert resp.json()["detail"], kind
    assert expected["capability_missing"] < 500


def test_the_detail_never_echoes_the_service_message(wired):
    wired["pr"].list_pull_requests.side_effect = GitHubPrError(
        f"merge conflict in acme/claims-triage at {ACCOUNT_SHAPED}", kind="conflict"
    )

    resp = _call(wired, "get", LIST_URL)

    assert ACCOUNT_SHAPED not in resp.json()["detail"]
    assert "acme" not in resp.json()["detail"]


def test_a_self_approval_is_refused_with_a_reason_and_no_retry(wired):
    wired["pr"].approve_pull_request.side_effect = GitHubPrError("own PR", kind="self_approval")

    resp = _call(wired, "post", APPROVE_URL)

    assert resp.status_code == 409
    assert resp.json()["detail"] == "you cannot approve your own pull request"
    wired["connection"].get_bearer_token.assert_not_called()
    assert wired["pr"].approve_pull_request.call_count == 1


def test_a_merge_refusal_surfaces_a_safe_hint(wired):
    wired["pr"].merge_pull_request.side_effect = GitHubPrError(
        f"Head branch was modified by {ACCOUNT_SHAPED}", kind="not_mergeable"
    )

    resp = _call(wired, "post", MERGE_URL)

    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert ACCOUNT_SHAPED not in detail
    assert detail == "this pull request cannot be merged yet"


def test_the_list_returns_the_view_shape_the_client_declares(wired):
    """Field for field the shape the frontend declares; a renamed field reads as ``undefined``."""
    wired["pr"].list_pull_requests.return_value = [_view(approve_blocked_reason=None, mergeable=None)]

    resp = _call(wired, "get", LIST_URL)

    assert resp.status_code == 200
    assert set(resp.json()[0]) == {
        "number",
        "title",
        "state",
        "author",
        "head_sha",
        "url",
        "can_approve",
        "approve_blocked_reason",
        "mergeable",
        "merge_commit_sha",
    }


def test_create_forwards_the_body_and_answers_201(wired):
    wired["pr"].create_pull_request.return_value = _view()

    resp = _call(wired, "post", LIST_URL, {"title": "t", "head": "feature/x", "base": "release", "body": "b"})

    assert resp.status_code == 201
    kwargs = wired["pr"].create_pull_request.call_args.kwargs
    assert kwargs["title"] == "t"
    assert kwargs["head"] == "feature/x"
    assert kwargs["base"] == "release"
    assert kwargs["body"] == "b"


def test_create_rejects_a_blank_title_before_any_provider_call(wired):
    resp = _call(wired, "post", LIST_URL, {"title": "   ", "head": "feature/x"})

    assert resp.status_code == 422
    wired["pr"].create_pull_request.assert_not_called()


def test_approve_and_merge_return_the_refreshed_view(wired):
    wired["pr"].approve_pull_request.return_value = _view(can_approve=False)
    wired["pr"].merge_pull_request.return_value = _view(state="merged", can_approve=False)

    approved = _call(wired, "post", APPROVE_URL)
    merged = _call(wired, "post", MERGE_URL)

    assert approved.status_code == 200
    assert merged.status_code == 200
    assert merged.json()["state"] == "merged"


def test_the_pr_number_is_a_path_param_the_route_forwards(wired):
    wired["pr"].approve_pull_request.return_value = _view()

    _call(wired, "post", "/api/v1/repositories/repo-1/pull-requests/42/approve")

    assert wired["pr"].approve_pull_request.call_args.args[2] == 42


def test_the_org_and_repo_name_come_from_the_records_never_the_caller(wired):
    """Accepting either from the caller would make this a write-to-any-repo primitive under a
    human's own token."""
    wired["pr"].list_pull_requests.return_value = [_view()]

    _call(wired, "get", LIST_URL)

    org, repo, _token = wired["pr"].list_pull_requests.call_args.args
    assert org == "acme"
    assert repo == "claims-triage"
