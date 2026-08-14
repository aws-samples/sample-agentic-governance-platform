"""Pull-request verbs on a repository (E28/T14 — design D14+D15, contract C2).

TWO LAYERS, and the split is the point.

**Layer 1 — the service** (:mod:`services.github_pr_service`) over an ``httpx.MockTransport``,
the ``test_github_repo_service`` idiom. It owns the provider conversation: which endpoint each
verb calls, how a GitHub PR object is PROJECTED onto :class:`~models.repository.PullRequestView`,
and which failures become which ``.kind``.

**Layer 2 — the routes** on the existing ``repositories_router``, the
``test_projects_role_gating`` harness verbatim (real ``require_role`` /
``current_principal`` / ``get_tenant_ctx`` / ``get_project_ctx`` against a mocked
``verify_entra_token``; the services as ``MagicMock``s on the module globals). It owns
composition: the gate, the 404-not-403 contract, and — the invariant this whole task exists
for — that the ONLY credential reaching the service is the E27B LINKED HUMAN's token.

The load-bearing invariants, and why each is pinned rather than assumed:

1. **NO APP-TOKEN FALLBACK, on any path (D15).** AGP holds an org-scoped App installation
   token that can open and approve PRs as *itself*. If a missing/broken user link ever fell
   back to it, AGP would approve a human's PR on their behalf — which silently defeats the
   self-approval refusal below, because the App is never the author. So: the service module
   must not even be able to reach an App token (asserted over its source), and the route must
   refuse rather than retry (asserted by call count on the connection service).
2. **A self-approval is REFUSED WITH A REASON, before GitHub is called.** GitHub answers 422
   for a review on your own PR; discovering it there would mean the affordance was offered.
   ``can_approve`` is computed from the linked human's own login, and ``approve`` refuses
   locally — with NO second attempt under any other credential.
3. **A MISSING GRANT IS A HIDDEN TAB, NOT A 500.** The org's App installation may not carry
   ``pull_requests: write`` (it is a manual per-org grant and GitHub does not retro-apply a
   manifest change). GitHub answers ``403`` for that, and the route maps it to ONE fixed
   literal the frontend resolves to a hidden tab. An unmapped kind — a 500 — would render a
   broken tab instead of no tab.
4. **A merge failure surfaces a SAFE HINT ONLY.** Fixed literals keyed off the status code;
   the provider body never crosses, because it can echo attacker-influenced content and this
   epic has already had to truncate one field for carrying an AWS account id.
5. **``mergeable`` is only ever trusted as ``False``.** It is typed three-valued, but the LIST
   endpoint this tab reads — ``GET /repos/{o}/{r}/pulls`` — OMITS the key entirely (proven live;
   only ``GET /pulls/{n}`` computes it), and the projection folds an omitted key and GitHub's own
   in-flight ``null`` into one ``None``. So "never asked" and "still computing" are deliberately
   not tellable apart, and no surface may claim either. What holds is the direction that matters:
   absent is never ``False``, because that reading suppresses a merge that would have worked.

No boto3, no live AWS, no live GitHub.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from models.project import Project, ProjectDetail
from models.repository import Repository
from services.github_pr_service import GitHubPrError, GitHubPrService

FIXED_TS = "2026-07-31T00:00:00+00:00"
VIEWER_LOGIN = "lars-svensson"


# ===========================================================================
# Layer 1 — the service, over httpx.MockTransport
# ===========================================================================


def _pr_payload(**over) -> dict:
    """A GitHub PR object, trimmed to the keys the projection reads.

    **``mergeable`` IS DELIBERATELY ABSENT**, because that is the shape live GitHub produces on
    the path this fixture mostly models. ``GET /repos/{o}/{r}/pulls`` — the LIST read behind the
    tab — omits the key entirely; only ``GET /pulls/{n}`` computes it. Supplying ``True`` here by
    default made every list test assert against a body GitHub never sends, and that fiction is
    what let a per-row "still being checked" hint ship and then say so on every row forever.
    Tests modelling a SINGLE-PR body pass ``mergeable=`` explicitly."""
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


# --- the projection --------------------------------------------------------


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
    # `None`, not `True` — the LIST endpoint does not send `mergeable` at all, and this fixture
    # now models that. Proven live: the list body omits the key while `GET /pulls/{n}` answers
    # `mergeable: true, mergeable_state: "clean"` for the same pull request.
    assert row.mergeable is None


def test_the_user_token_is_the_only_authorization_sent():
    """The token is a per-request Bearer header, the ``github_repo_service`` idiom — and it is
    the LINKED HUMAN's. Nothing else authenticates the call."""
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["auth"] = req.headers.get("authorization")
        seen["accept"] = req.headers.get("accept")
        return httpx.Response(200, json=[])

    _svc(handler).list_pull_requests("acme", "r", "ghu_user_token", viewer_login=VIEWER_LOGIN)

    assert seen["auth"] == "Bearer ghu_user_token"
    assert seen["accept"] == "application/vnd.github+json"


def test_a_merged_pr_reports_merged_not_closed():
    """GitHub's ``state`` is only ``open``/``closed`` — a merged PR is ``closed`` there. Showing
    a shipped PR as merely "closed" loses the one distinction that matters on this surface."""
    rows = _svc(
        _ok([_pr_payload(state="closed", merged_at="2026-07-31T09:00:00Z")])
    ).list_pull_requests("acme", "r", "t", viewer_login=VIEWER_LOGIN)

    assert rows[0].state == "merged"


def test_a_genuinely_closed_pr_stays_closed():
    rows = _svc(_ok([_pr_payload(state="closed", merged_at=None)])).list_pull_requests(
        "acme", "r", "t", viewer_login=VIEWER_LOGIN
    )

    assert rows[0].state == "closed"


@pytest.mark.parametrize("payload", [{}, {"mergeable": None}], ids=["absent", "explicit-null"])
def test_an_absent_and_an_explicit_null_mergeable_both_project_to_none(payload):
    """Neither is ``False``, and — the point of this test — the two are INDISTINGUISHABLE after
    projection, on purpose.

    ``_project`` folds anything non-boolean to ``None``, so a body that omitted the key (what the
    LIST endpoint always sends) and one that answered ``null`` (a single-PR read whose async
    computation is still in flight) become one value. That fold is why no surface may render "the
    provider is still working it out": nothing downstream can tell that case from "nobody ever
    asked". What IS still guaranteed is the direction that matters — never ``False``, because
    claiming a clean pull request cannot be merged is the one reading that suppresses a button
    that would have worked."""
    rows = _svc(_ok([_pr_payload(**payload)])).list_pull_requests(
        "acme", "r", "t", viewer_login=VIEWER_LOGIN
    )

    assert rows[0].mergeable is None
    assert rows[0].mergeable is not False


def test_a_single_pr_read_still_carries_a_real_mergeable():
    """The field is not dead: ``GET /pulls/{n}`` — the read behind create/approve/merge — DOES
    compute it, and there ``True``/``False`` are truthful. Only the list path's silence is."""
    view = _svc(_ok(_pr_payload(mergeable=True))).create_pull_request(
        "acme", "r", "t", viewer_login=VIEWER_LOGIN, title="t", head="h", base=None, body=None
    )

    assert view.mergeable is True

    unmergeable = _svc(_ok(_pr_payload(mergeable=False))).create_pull_request(
        "acme", "r", "t", viewer_login=VIEWER_LOGIN, title="t", head="h", base=None, body=None
    )

    assert unmergeable.mergeable is False


def test_a_pr_with_no_author_object_reads_as_absent_never_as_the_viewer():
    """A deleted GitHub account leaves ``user: null``. Defaulting the author to the VIEWER would
    make ``can_approve`` false for a PR they did not write; defaulting it to any other login
    would assert an identity nobody established. Absent is the honest projection."""
    rows = _svc(_ok([_pr_payload(user=None)])).list_pull_requests(
        "acme", "r", "t", viewer_login=VIEWER_LOGIN
    )

    assert rows[0].author == ""
    # And with no author there is nobody to be, so nothing establishes a self-approval.
    assert rows[0].can_approve is True


def test_a_malformed_row_is_skipped_not_crashed():
    """A provider list whose entries are not objects must not 500 the whole tab. One unusable
    row is dropped; the readable ones are still served."""
    rows = _svc(_ok(["nonsense", _pr_payload(), {"no": "number"}])).list_pull_requests(
        "acme", "r", "t", viewer_login=VIEWER_LOGIN
    )

    assert [r.number for r in rows] == [7]


def test_a_non_list_body_is_empty_not_an_exception():
    rows = _svc(_ok({"message": "surprise"})).list_pull_requests(
        "acme", "r", "t", viewer_login=VIEWER_LOGIN
    )

    assert rows == []


# --- can_approve (D15) ----------------------------------------------------


def test_can_approve_is_false_when_the_linked_human_is_the_author():
    """THE headline refusal. GitHub refuses a review on your own PR, and AGP acts AS the human —
    so the refusal is stated before the affordance is offered, not discovered as a 422."""
    rows = _svc(_ok([_pr_payload(user={"login": VIEWER_LOGIN})])).list_pull_requests(
        "acme", "r", "t", viewer_login=VIEWER_LOGIN
    )

    assert rows[0].can_approve is False
    assert rows[0].approve_blocked_reason
    # A REASON, not an error — the UI states it calmly. And it names the actual cause.
    assert "own" in rows[0].approve_blocked_reason.lower()


def test_the_author_comparison_is_case_insensitive():
    """GitHub logins are case-insensitive, so ``Lars-Svensson`` and ``lars-svensson`` are one
    human. A case-sensitive compare would OFFER a self-approval the provider then 422s."""
    rows = _svc(_ok([_pr_payload(user={"login": VIEWER_LOGIN.upper()})])).list_pull_requests(
        "acme", "r", "t", viewer_login=VIEWER_LOGIN
    )

    assert rows[0].can_approve is False


def test_a_closed_or_merged_pr_cannot_be_approved_either():
    """An affordance whose every click is refused is not an affordance: GitHub takes no review
    on a closed PR. Stated as its own reason, distinct from the self-approval one."""
    for over in [{"state": "closed"}, {"state": "closed", "merged_at": "2026-07-31T09:00:00Z"}]:
        rows = _svc(_ok([_pr_payload(**over)])).list_pull_requests(
            "acme", "r", "t", viewer_login=VIEWER_LOGIN
        )
        assert rows[0].can_approve is False, over
        assert rows[0].approve_blocked_reason
        assert "own" not in rows[0].approve_blocked_reason.lower(), over


def test_an_unknown_viewer_login_refuses_rather_than_permits():
    """A blank viewer login means AGP does not know WHO it would be acting as. Permitting the
    approve then risks exactly the self-approval this refuses — so absent standing is refused,
    the fail-closed direction."""
    rows = _svc(_ok([_pr_payload()])).list_pull_requests("acme", "r", "t", viewer_login="")

    assert rows[0].can_approve is False
    assert rows[0].approve_blocked_reason


def test_approve_refuses_a_self_approval_without_calling_the_review_endpoint():
    """No second attempt, under ANY credential. The refusal happens before the write, and the
    review endpoint is never reached — which is what makes 'no App-token retry' observable."""
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append((req.method, req.url.path))
        return httpx.Response(200, json=_pr_payload(user={"login": VIEWER_LOGIN}))

    with pytest.raises(GitHubPrError) as ei:
        _svc(handler).approve_pull_request(
            "acme", "claims-triage", 7, "ghu_user_token", viewer_login=VIEWER_LOGIN
        )

    assert ei.value.kind == "self_approval"
    # Read the PR to decide — then STOP. No review POST, and nothing retried.
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
    # The answer is a FRESH read, so the caller sees the PR as it now is rather than as the
    # optimistic copy the client sent.
    assert calls[-1] == ("GET", "/repos/acme/claims-triage/pulls/7")
    assert view.number == 7


def test_approve_maps_the_providers_own_self_review_refusal_too():
    """Belt and braces: if the local check were ever bypassed (a login AGP could not resolve,
    say), GitHub's own 422 must still read as the same refusal — never as a server fault."""

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            return httpx.Response(422, json={"message": "Can not approve your own pull request"})
        return httpx.Response(200, json=_pr_payload())

    with pytest.raises(GitHubPrError) as ei:
        _svc(handler).approve_pull_request("acme", "r", 7, "t", viewer_login="someone-else")

    assert ei.value.kind == "self_approval"


# --- create ---------------------------------------------------------------


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
    """NO stage-shaped default (D8): a tenant's branch set is open, so an omitted base means
    'the repository's own default branch' — resolved by the PROVIDER, never guessed here. A
    literal default would be the same hardcode the design forbids one layer down."""
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(req.content)
        return httpx.Response(201, json=_pr_payload())

    _svc(handler).create_pull_request(
        "acme", "r", "t", viewer_login=VIEWER_LOGIN, title="t", head="h", base=None, body=None
    )

    assert "base" not in seen["body"]
    # An absent body is absent, not an empty string GitHub would render as a blank comment.
    assert "body" not in seen["body"]


def test_create_maps_a_duplicate_pr_to_conflict():
    """A PR for that branch pair already exists — an ordinary state the UI renders, never a
    server fault."""

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
    """The 422 an operator meets by ACCIDENT — opening a PR from a branch that is not ahead.

    Found live: this bucketed into ``conflict``, whose copy told the operator a pull request may
    already exist and the branch may have moved. Neither was true (the repo had zero PRs), so the
    surface asserted two causes it had never established. Its own kind is what lets the UI state
    the one cause GitHub actually gave.

    Note WHERE GitHub says it: ``errors[].message``, not ``message`` — which is why
    ``_message_of`` concatenates both. A matcher reading only ``message`` would see nothing here
    and silently fall back to the generic bucket, restoring the defect."""

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
    # …and it still carries no provider text: the classification crosses, the body does not.
    assert "main" not in str(ei.value)
    assert "dev" not in str(ei.value)


# --- merge ----------------------------------------------------------------


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
    """SAFE HINT ONLY. A provider body can echo attacker-influenced content, and this epic has
    already had to truncate a field for carrying an AWS account id."""
    secret_ish = "Head branch was modified by 111122223333/acme"

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "PUT":
            return httpx.Response(409, json={"message": secret_ish})
        return httpx.Response(200, json=_pr_payload())

    with pytest.raises(GitHubPrError) as ei:
        _svc(handler).merge_pull_request("acme", "r", 7, "t", viewer_login=VIEWER_LOGIN)

    text = str(ei.value)
    assert secret_ish not in text
    assert "111122223333" not in text
    # …and it is still a usable hint, not an empty string.
    assert len(text) > 10


# --- the capability probe (A3) --------------------------------------------


def test_a_403_on_the_list_is_the_missing_grant_signal():
    """The org's App installation may not carry ``pull_requests: write`` — a MANUAL per-org
    grant that GitHub does not retro-apply from a manifest change. GitHub answers 403
    ("Resource not accessible by integration"), and that is the signal the frontend resolves to
    a HIDDEN tab. It must be its own kind, distinguishable from every other failure."""

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
    """A transport exception's args can carry a URL with auth material, so only the exception
    TYPE crosses — the ``github_repo_service`` / ``github_user_oauth`` rule."""

    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("failed to connect to https://x?token=ghu_leak")

    with pytest.raises(GitHubPrError) as ei:
        _svc(handler).list_pull_requests("acme", "r", "t", viewer_login=VIEWER_LOGIN)

    assert ei.value.kind == "provider_error"
    assert "ghu_leak" not in str(ei.value)
    assert "ConnectError" in str(ei.value)


def test_no_error_message_anywhere_carries_the_token():
    """Every failure path, one assertion: the credential is never folded into a message."""
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


# --- the structural no-App-token guard ------------------------------------


def test_the_service_module_cannot_reach_an_app_token():
    """NO APP-TOKEN FALLBACK, made structural rather than promised (D15).

    AGP holds an org-scoped App installation token that can open and approve PRs as *itself*.
    If this module could reach one, a broken user link could silently fall back to it — and
    since the App is never the author, the self-approval refusal above would be bypassed
    entirely: AGP would approve a human's own PR on their behalf.

    So the module takes its token as a PARAMETER and has no way to mint or load one. Asserted
    over the RAW source (no lowercasing, no comment stripping — a normalization step is how a
    guard stops seeing what it guards), because an import added later is exactly the change a
    reviewer would have to catch by eye."""
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


# ===========================================================================
# Layer 2 — the routes on repositories_router
# ===========================================================================


@pytest.fixture(autouse=True)
def reset_modules():
    import sys

    for mod in [
        "core.rbac",
        "core.security_entra",
        "core.config",
        "api.routes.projects",
        "api.routes.users",
    ]:
        sys.modules.pop(mod, None)
    yield


@pytest.fixture
def entra_settings(monkeypatch):
    monkeypatch.setenv("AUTH_PROVIDER", "entra")
    monkeypatch.setenv("USE_DEV_AUTH", "False")
    monkeypatch.setenv("DEBUG", "False")
    monkeypatch.setenv("ENTRA_TENANT_ID", "00000000-0000-0000-0000-000000000001")
    monkeypatch.setenv("ENTRA_AUDIENCE", "api://agp")
    monkeypatch.setenv("ENTRA_ROLE_ADMIN", "Platform.Admin")
    monkeypatch.setenv("ENTRA_ROLE_OPERATOR", "Platform.Operator")
    monkeypatch.setenv("ENTRA_ROLE_VIEWER", "Platform.Viewer")


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
    """The four collaborators the PR routes compose, as MagicMocks on the module globals.

    Defaults are the HAPPY path — a visible project, a repo under it, a live GitHub link, and
    a PR service that answers — so each test perturbs exactly one of them."""
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


def _client(wired, *, project_role="maintainer", is_global=False):
    import api.routes.projects as projects_module
    import api.routes.users as users_module

    users_module._tenant_resolver = _FakeTenantResolver(_tenant_context())
    users_module._project_resolver = _FakeProjectResolver(
        _project_context(is_global=is_global, role=project_role)
    )
    app = FastAPI()
    app.include_router(projects_module.repositories_router, prefix="/api/v1")
    return TestClient(app, headers={"Authorization": "Bearer fake-token"})


def _call(wired, method, path, body=None, *, project_role="maintainer", is_global=False):
    client = _client(wired, project_role=project_role, is_global=is_global)
    with patch(
        "core.security_entra.verify_entra_token", return_value=_claims()
    ), patch("api.routes.connections.get_connection_service", return_value=wired["connection"]), patch(
        "api.routes.github_link.get_github_link_service", return_value=wired["link"]
    ), patch(
        "api.routes.projects.get_pr_service", return_value=wired["pr"]
    ):
        return getattr(client, method)(path, **({"json": body} if body is not None else {}))


LIST_URL = "/api/v1/repositories/repo-1/pull-requests"
APPROVE_URL = "/api/v1/repositories/repo-1/pull-requests/7/approve"
MERGE_URL = "/api/v1/repositories/repo-1/pull-requests/7/merge"
CREATE_BODY = {"title": "t", "head": "feature/x"}


def _view(**over):
    from models.repository import PullRequestView

    payload = {
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


# --- the paths match the pinned client calls (A1) -------------------------


def test_the_four_routes_are_mounted_at_the_pinned_paths():
    """C2 + A1: these four paths are what ``client.ts``'s ``pullRequestsApi`` already calls.
    They 404'd until this task mounted them; if a path here drifts, the frontend 404s again and
    nothing else notices."""
    import api.routes.projects as projects_module

    mounted = {
        (m, r.path)
        for r in projects_module.repositories_router.routes
        for m in getattr(r, "methods", set())
    }
    assert ("GET", "/repositories/{repo_id}/pull-requests") in mounted
    assert ("POST", "/repositories/{repo_id}/pull-requests") in mounted
    assert ("POST", "/repositories/{repo_id}/pull-requests/{number}/approve") in mounted
    assert ("POST", "/repositories/{repo_id}/pull-requests/{number}/merge") in mounted


def test_create_answers_201():
    import api.routes.projects as projects_module

    route = next(
        r
        for r in projects_module.repositories_router.routes
        if getattr(r, "path", "") == "/repositories/{repo_id}/pull-requests"
        and "POST" in getattr(r, "methods", set())
    )
    assert route.status_code == 201


def test_the_four_pr_routes_carry_a_gate_identical_to_the_routers_existing_read():
    """Mechanical, not by eye. ``GET /repositories`` is the sibling already on this router; two
    reads of one resource must not carry different guards, because whichever is looser IS the
    gate (D3 — guards compose on one handler, never clone a route to change one). A hand-copied
    gate is exactly how a pair drifts."""
    import api.routes.projects as projects_module
    from core.rbac import Role

    def deps(path, method):
        route = next(
            r
            for r in projects_module.repositories_router.routes
            if getattr(r, "path", "") == path and method in getattr(r, "methods", set())
        )
        out = []
        for d in route.dependant.dependencies:
            captured = tuple(c.cell_contents for c in (d.call.__closure__ or ()))
            out.append((d.name, d.call.__qualname__, captured))
        return out

    existing = deps("/repositories", "GET")
    # Guards this against a vacuous pass: the existing read really does carry all four halves,
    # and the role half really did capture OPERATOR.
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


# --- the gate --------------------------------------------------------------


def test_a_foreign_tenants_repo_is_byte_identically_absent(wired):
    """404-not-403: a repo whose parent project belongs to another tenant must look ABSENT, and
    identically so to a truly-missing id — a 403 would confirm it exists. And no provider call
    happens before the gate has run."""
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
    # …and no credential was even resolved for a repo the caller may not see.
    wired["link"].get_user_bearer_token.assert_not_called()


def test_a_repo_whose_project_vanished_is_absent_not_a_500(wired):
    wired["project"].get_project.return_value = None

    resp = _call(wired, "get", LIST_URL)

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Repository not found"


def test_reading_the_list_needs_viewer_and_writing_needs_maintainer(wired):
    """A read is a VIEWER act; opening, approving and merging a PR are MAINTAINER acts — the
    same threshold ``retry`` carries, and deliberately NOT promote's strict OWNER: a merge to
    the default branch registers a prod CANDIDATE, which still needs an OWNER's promote. So
    this cannot be a bypass of the prod gate."""
    wired["pr"].list_pull_requests.return_value = [_view()]
    wired["pr"].create_pull_request.return_value = _view()
    wired["role"].has_role_rows.return_value = True  # governed ⇒ no §3 fallback

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
    """The refusal lands BEFORE the credential is resolved and before GitHub is called — a
    403 that has already opened a PR is not a refusal."""
    wired["role"].has_role_rows.return_value = True

    _call(wired, "post", LIST_URL, CREATE_BODY, project_role="viewer")

    wired["pr"].create_pull_request.assert_not_called()
    wired["link"].get_user_bearer_token.assert_not_called()


def test_a_platform_admin_needs_no_project_role(wired):
    wired["pr"].list_pull_requests.return_value = [_view()]

    resp = _call(wired, "get", LIST_URL, project_role=None, is_global=True)

    assert resp.status_code == 200


# --- NO APP-TOKEN FALLBACK, at the composition layer ----------------------


def test_the_only_credential_that_reaches_the_service_is_the_linked_users(wired):
    """Composition-layer half of the D15 invariant. The service takes its token as a parameter;
    THIS asserts which token the route actually hands it, and that the App-token seam
    (``ConnectionService.get_bearer_token``) is never touched on any PR path."""
    wired["pr"].list_pull_requests.return_value = [_view()]

    assert _call(wired, "get", LIST_URL).status_code == 200

    wired["link"].get_user_bearer_token.assert_called_once_with("lars-oid", "conn-1")
    _org, _repo, token = wired["pr"].list_pull_requests.call_args.args
    assert token == "ghu_user_token"
    wired["connection"].get_bearer_token.assert_not_called()


def test_a_missing_link_refuses_and_does_NOT_retry_under_an_app_token(wired):
    """The refusal this whole task is shaped around. A human with no GitHub link cannot act as
    themselves — so AGP does nothing, rather than acting as ITSELF. Falling back to the App
    token would let AGP approve a human's own PR (the App is never the author), which silently
    defeats the self-approval refusal entirely."""
    from services.github_user_link import GitHubLinkError

    wired["link"].get_user_bearer_token.side_effect = GitHubLinkError("no row", kind="not_found")

    resp = _call(wired, "get", LIST_URL)

    assert resp.status_code == 409
    assert "GitHub account" in resp.json()["detail"]
    # NOT retried under any other credential, and the provider was never reached.
    wired["connection"].get_bearer_token.assert_not_called()
    wired["pr"].list_pull_requests.assert_not_called()


def test_a_revoked_link_refuses_the_same_way(wired):
    from services.github_user_link import GitHubLinkError

    wired["link"].get_user_bearer_token.side_effect = GitHubLinkError(
        "revoked", kind="link_revoked"
    )

    resp = _call(wired, "get", LIST_URL)

    assert resp.status_code == 409
    wired["connection"].get_bearer_token.assert_not_called()


def test_the_viewer_login_comes_from_the_link_row_not_from_the_caller(wired):
    """``can_approve`` is decided against WHO AGP is acting as, and that is the GitHub login on
    the E27B link row — a provider currency. It is never derived from the Entra principal, which
    is a different currency with no mapping to it (E27A §6)."""
    wired["pr"].list_pull_requests.return_value = [_view()]

    _call(wired, "get", LIST_URL)

    assert wired["pr"].list_pull_requests.call_args.kwargs["viewer_login"] == VIEWER_LOGIN


def test_a_link_on_another_org_is_not_used_for_this_one(wired):
    """A human may link several orgs. The row for THIS repo's connection is the only one whose
    login may decide ``can_approve`` — reading another org's login would compare a stranger."""
    other = _link(login="someone-else")
    other.connection_id = "conn-other"
    wired["link"].list_for_principal.return_value = [other]
    wired["pr"].list_pull_requests.return_value = [_view()]

    _call(wired, "get", LIST_URL)

    assert wired["pr"].list_pull_requests.call_args.kwargs["viewer_login"] == ""


# --- the capability refusal is a HIDDEN tab, not a 500 (A3) ---------------


def test_a_missing_org_grant_is_a_mapped_403_never_a_500(wired):
    """The org's App installation does not carry ``pull_requests: write`` — a MANUAL per-org
    grant GitHub does not retro-apply. It must answer ONE fixed literal the frontend resolves
    to a HIDDEN tab. An unmapped kind would be a 500, which renders a BROKEN tab instead of no
    tab — and a broken tab is the outcome the requirement names."""
    wired["pr"].list_pull_requests.side_effect = GitHubPrError("nope", kind="capability_missing")

    resp = _call(wired, "get", LIST_URL)

    assert resp.status_code == 403
    assert resp.json()["detail"] == "pull requests are not enabled for this organization"


def test_every_service_kind_is_mapped_so_none_reaches_an_unhandled_500(wired):
    """The mapping is EXHAUSTIVE by test. An UNMAPPED kind escapes as an unhandled 500, and on
    the list read that is precisely the crash the hidden-tab requirement forbids.

    ``provider_error`` is deliberately the ONE kind that answers 5xx, and it answers **502** —
    the house idiom (``github_link.py``'s ``_ERROR_STATUS``) for "the upstream failed". That is
    honest and is not the failure this requirement is about: a real GitHub outage is not a
    missing grant, and reporting it as a client-side state would tell the operator the tab is
    unavailable when it is GitHub that is down. What must never be 5xx is the CAPABILITY
    refusal, asserted separately above and re-asserted here."""
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
        # An UNMAPPED kind would fall through to the same fixed 502 as provider_error, which
        # would make this table vacuous — so assert the mapping really discriminates.
        assert resp.json()["detail"], kind
    # The hidden-tab case specifically: never 5xx, whatever else changes here.
    assert expected["capability_missing"] < 500


# --- the mechanical kind ↔ mapping guard ----------------------------------


def _kinds_declared_in(source: str) -> "set[str]":
    """Every ``kind`` VALUE the given module source can actually produce.

    Reads the PARSE TREE, not the text. That choice is the whole guard: E28 shipped five
    source-as-text guards that their own explanatory comment defeated, because a comment quoting
    the forbidden string satisfied the substring search. A comment cannot be a ``keyword`` node,
    so prose mentioning a kind — including this docstring — contributes nothing.

    THREE producing forms, because the module uses all three:

    1. An explicit ``kind="…"`` argument at a call site.
    2. The ``kind`` PARAMETER DEFAULT on ``GitHubPrError.__init__`` — a raise that passes no kind
       still produces that value, so a guard blind to it would miss a renamed default.
    3. Strings assigned to a local named ``kind``, which is how ``approve_pull_request`` picks
       between ``self_approval`` and ``not_approvable`` with a conditional expression before
       passing ``kind=kind``. The first version of this guard saw only forms 1 and 2 and reported
       ``not_approvable`` as a dead mapping entry — a false accusation, and proof that a
       narrower extractor is not a safer one."""
    tree = ast.parse(source)
    kinds = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "kind":
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                kinds.add(node.value.value)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(t, ast.Name) and t.id == "kind" for t in targets):
                for inner in ast.walk(node.value) if node.value is not None else []:
                    if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
                        kinds.add(inner.value)
        elif isinstance(node, ast.arguments):
            named = [*node.posonlyargs, *node.args, *node.kwonlyargs]
            defaults = [
                *([None] * (len(node.posonlyargs) + len(node.args) - len(node.defaults))),
                *node.defaults,
                *node.kw_defaults,
            ]
            for arg, default in zip(named, defaults):
                if (
                    arg.arg == "kind"
                    and isinstance(default, ast.Constant)
                    and isinstance(default.value, str)
                ):
                    kinds.add(default.value)
    return kinds


def test_every_kind_the_service_can_produce_has_a_route_mapping_and_vice_versa():
    """The mapping above is EXHAUSTIVE by hand, and that is not enough on its own.

    ``_raise_pr_error`` does ``_PR_ERROR.get(err.kind, _PR_ERROR["provider_error"])``, so a NEW
    service kind with no entry does not fail loudly — it silently becomes the 502 "GitHub request
    failed", which the frontend renders as "GitHub could not be reached. Nothing was changed."
    That is a fresh honesty defect of exactly the class this task removed: a surface stating a
    cause nobody established. The hand-maintained table in the test above would still pass,
    because it only checks the kinds it happens to list.

    So the two sets are compared MECHANICALLY, in both directions. The reverse direction matters
    too: a mapping entry no kind produces is dead copy that reads as live."""
    import api.routes.projects as projects_module

    source = Path("src/services/github_pr_service.py").read_text()
    assert len(source) > 500, "read the real module, not an empty file"

    produced = _kinds_declared_in(source)
    mapped = set(projects_module._PR_ERROR)

    assert produced, "the extractor found no kinds at all — it has stopped seeing the module"
    assert produced - mapped == set(), (
        f"service kinds with no _PR_ERROR entry (they silently become a 502): {produced - mapped}"
    )
    assert mapped - produced == set(), (
        f"_PR_ERROR entries no service kind produces: {mapped - produced}"
    )


def test_that_guard_cannot_be_satisfied_by_PROSE_mentioning_a_kind():
    """The failure mode that defeated five of E28's guards, asserted against directly.

    A comment or docstring quoting a kind must contribute NOTHING — otherwise documenting a kind
    would be indistinguishable from producing one, and the guard above would pass on a module
    that only talks about the kind it forgot to map. Also pins the two forms that DO count, so
    the extractor cannot regress to matching nothing at all (a guard that sees nothing passes
    everything)."""
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
    # Form 3 — the conditional local ``approve_pull_request`` actually uses. Both arms count:
    # either can reach the raise, so seeing only one would report the other as dead copy.
    assert _kinds_declared_in('kind = "left_arm" if flag else "right_arm"') == {
        "left_arm",
        "right_arm",
    }
    # …and a string assigned to some OTHER name is not a kind.
    assert _kinds_declared_in('reason = "not_a_kind_at_all"') == set()


def test_the_kind_cannot_be_passed_POSITIONALLY_so_the_guard_has_no_SECOND_DOOR():
    """``kind`` is keyword-only, which is what makes the guard above STRUCTURALLY complete.

    The extractor sees ``kind=`` keywords, the parameter default, and strings assigned to a local
    named ``kind``. It cannot see ``GitHubPrError("x", "new_kind")`` — with a positional-capable
    signature that is ordinary Python, the extractor returns ``set()`` for it, the guard passes,
    and the unmapped kind silently ships the 502 "GitHub request failed" on a request GitHub
    answered perfectly well. That is the exact honesty defect the guard exists to prevent,
    reachable through a door the guard does not watch.

    Closing it at the signature is stronger than teaching the extractor a fourth form: the
    positional call cannot be written at all, so the three forms are exhaustive by construction
    rather than by convention."""
    with pytest.raises(TypeError):
        GitHubPrError("x", "new_kind")  # type: ignore[misc]

    # The keyword form is unaffected — every call site in src/ and tests/ uses it.
    assert GitHubPrError("x", kind="new_kind").kind == "new_kind"
    # …and the default still applies when omitted.
    assert GitHubPrError("x").kind == "provider_error"


def test_the_detail_never_echoes_the_service_message(wired):
    """FIXED literals keyed off ``.kind`` — never ``str(err)``, the repo-wide rule. A provider
    message must not become an HTTP body."""
    wired["pr"].list_pull_requests.side_effect = GitHubPrError(
        "merge conflict in acme/claims-triage at 111122223333", kind="conflict"
    )

    resp = _call(wired, "get", LIST_URL)

    assert "111122223333" not in resp.json()["detail"]
    assert "acme" not in resp.json()["detail"]


def test_a_self_approval_is_refused_with_a_reason_and_no_retry(wired):
    """D15 at the route: a 409 the UI states calmly, and NOTHING retried underneath it."""
    wired["pr"].approve_pull_request.side_effect = GitHubPrError(
        "own PR", kind="self_approval"
    )

    resp = _call(wired, "post", APPROVE_URL)

    assert resp.status_code == 409
    assert resp.json()["detail"] == "you cannot approve your own pull request"
    wired["connection"].get_bearer_token.assert_not_called()
    assert wired["pr"].approve_pull_request.call_count == 1


def test_a_merge_refusal_surfaces_a_safe_hint(wired):
    wired["pr"].merge_pull_request.side_effect = GitHubPrError(
        "Head branch was modified by 111122223333", kind="not_mergeable"
    )

    resp = _call(wired, "post", MERGE_URL)

    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "111122223333" not in detail
    assert detail == "this pull request cannot be merged yet"


# --- the happy paths ------------------------------------------------------


def test_the_list_returns_the_view_shape_the_client_declares(wired):
    """Field-for-field the shape ``client.ts:1056-1074`` declares (A1) — the pin. A field
    renamed here is a field the frontend silently reads as ``undefined``."""
    wired["pr"].list_pull_requests.return_value = [
        _view(approve_blocked_reason=None, mergeable=None)
    ]

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
    }


def test_create_forwards_the_body_and_answers_201(wired):
    wired["pr"].create_pull_request.return_value = _view()

    resp = _call(
        wired, "post", LIST_URL, {"title": "t", "head": "feature/x", "base": "release", "body": "b"}
    )

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
    """The provider coordinates are RESOLVED server-side from the repository record and its
    project's connection. Accepting either from the caller would make this a
    write-to-any-repo primitive under a human's own token."""
    wired["pr"].list_pull_requests.return_value = [_view()]

    _call(wired, "get", LIST_URL)

    org, repo, _token = wired["pr"].list_pull_requests.call_args.args
    assert org == "acme"
    assert repo == "claims-triage"
