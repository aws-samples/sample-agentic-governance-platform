"""Pull-request verbs on a GitHub repository, as the linked human.

A thin ``httpx`` client in the shape of :mod:`services.github_repo_service`: a base URL, a
per-request ``Authorization: Bearer {token}`` header, an injectable ``httpx.Client`` and safe error
messages. Unlike that module, which acts as AGP itself under an org-scoped App-minted token, every
verb here is a human's act attributed to a GitHub account, so this module takes its token as a
parameter and has no way to obtain one: it imports no credential seam, mints nothing and reads no
secret store. That is the mechanism behind the self-approval refusal: were an App token reachable,
a human with a broken link would fall back to it and AGP, never the author of anyone's pull
request, would approve the human's own. ``test_the_service_module_cannot_reach_an_app_token``
reads this file's source and fails on any such import.

Nothing is persisted; GitHub is the system of record and every verb reads through.
``can_approve`` is computed from the viewer's own login (case-insensitively, as GitHub logins are)
before the affordance is offered, :meth:`GitHubPrService.approve_pull_request` refuses locally
without a second attempt, and the provider's own 422 maps to the same refusal. A blank
``viewer_login`` fails closed. Provider bodies are read to classify a failure into a kind and then
discarded; a transport failure surfaces the exception type only, re-raised ``from None``.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Callable, ClassVar, List, Optional

import httpx

from core.errors import AppError, Outcome
from models.repository import PullRequestView

logger = logging.getLogger(__name__)

GITHUB_DEFAULT_BASE = "https://api.github.com"

_PER_PAGE = 30
_LIST_STATE = "all"

_REASON_SELF = "You opened this pull request — GitHub does not accept your own review."
_REASON_NOT_OPEN = "This pull request is no longer open, so it accepts no further review."
_REASON_NO_VIEWER = (
    "AGP could not establish which GitHub account it would review as. Link your GitHub "
    "account to review as yourself."
)


class GitHubPrError(AppError):
    """A pull-request operation failed. The message is fixed text plus at most an HTTP status,
    never the token or a provider body; the client sees the fixed detail of the kind.

    ``capability_missing`` is the org's App lacking the ``pull_requests`` grant (a manual per-org
    grant GitHub does not retro-apply to an existing App), which the frontend resolves to a
    hidden tab; ``self_approval`` is the author reviewing their own pull request;
    ``not_approvable`` is a closed pull request or an unresolvable viewer; ``not_mergeable`` is
    the provider refusing the merge; ``conflict`` is a request GitHub declined as asked (a
    duplicate, a stale head); ``no_commits`` is a head branch not ahead of the base;
    ``not_found`` is a repository or pull request this token cannot see; ``connection_unavailable``
    is the project's org connection gone or unreadable, so AGP cannot address the provider at all
    (the same wire as a provider failure, which is what the console shows for it); ``provider_error``
    is anything else, including a transport failure.

    ``kind`` is keyword-only, so a positional second argument is a ``TypeError`` rather than a
    kind the table never saw.
    """

    domain: ClassVar[str] = "github_pr"
    kinds: ClassVar[Mapping[str, Outcome]] = {
        "capability_missing": Outcome(403, "pull requests are not enabled for this organization"),
        "not_found": Outcome(404, "pull request not found"),
        "self_approval": Outcome(409, "you cannot approve your own pull request"),
        "not_approvable": Outcome(409, "this pull request cannot be approved"),
        "not_mergeable": Outcome(409, "this pull request cannot be merged yet"),
        "conflict": Outcome(409, "GitHub declined the request for this pull request"),
        "no_commits": Outcome(409, "there are no commits between these branches"),
        "provider_error": Outcome(502, "GitHub request failed"),
        "connection_unavailable": Outcome(502, "GitHub request failed"),
    }

    def __init__(self, message: str, *, kind: str = "provider_error") -> None:
        super().__init__(message, kind)


class GitHubPrDuplicateError(GitHubPrError):
    """The 422 "a pull request already exists" for this head. Its one row is the parent's
    ``conflict`` row, so the wire is unchanged, but a caller that just re-cut the head branch can
    tell it from every other 422."""

    kinds: ClassVar[Mapping[str, Outcome]] = {"conflict": GitHubPrError.kinds["conflict"]}

    def __init__(self, message: str, *, kind: str = "conflict") -> None:
        super().__init__(message, kind=kind)


def _message_of(resp: httpx.Response) -> str:
    """GitHub's ``message`` plus any ``errors[].message``, lowercased, for classification only;
    it is never folded into a :class:`GitHubPrError` message."""
    try:
        body = resp.json()
    except ValueError:
        return ""
    if not isinstance(body, dict):
        return ""
    parts = [body.get("message") or ""]
    errors = body.get("errors")
    if isinstance(errors, list):
        parts += [e.get("message") or "" for e in errors if isinstance(e, dict)]
    return " ".join(p for p in parts if p).lower()


def _text(value) -> str:
    """A trimmed string, or empty. Blank is absence, not data."""
    return value.strip() if isinstance(value, str) else ""


class GitHubPrService:
    """Pull-request verbs under a caller-supplied user token. ``client`` is injectable and carries
    no pre-set auth, so one client never leaks one human's token onto another's call."""

    def __init__(self, *, client: Optional[httpx.Client] = None) -> None:
        self._client = client
        self._owns_client = client is None

    def list_pull_requests(
        self,
        org: str,
        repo: str,
        token: str,
        *,
        viewer_login: str,
        base_url: Optional[str] = None,
    ) -> List[PullRequestView]:
        """The repo's pull requests projected onto :class:`PullRequestView`.

        A 403 here is the capability signal the frontend resolves to a hidden tab. A malformed
        row is skipped rather than fatal, and a non-list body answers ``[]``: a provider surprise
        is not a list of pull requests, but it is not a reason to 500 a page either."""
        resp = self._request(
            "GET",
            f"{self._base(base_url)}/repos/{org}/{repo}/pulls",
            token,
            what="list the pull requests",
            params={"state": _LIST_STATE, "per_page": _PER_PAGE},
        )
        try:
            body = resp.json()
        except ValueError:
            body = None
        if not isinstance(body, list):
            logger.warning("[pr] %s/%s answered a non-list pull-request body", org, repo)
            return []
        views = []
        for item in body:
            view = _project(item, viewer_login)
            if view is not None:
                views.append(view)
        return views

    def create_pull_request(
        self,
        org: str,
        repo: str,
        token: str,
        *,
        viewer_login: str,
        title: str,
        head: str,
        base: Optional[str],
        body: Optional[str],
        base_url: Optional[str] = None,
    ) -> PullRequestView:
        """Open a pull request as the linked human.

        ``base`` is omitted entirely when the caller named none, so "no base" means the
        repository's own default branch, a fact the provider holds; ``body`` is omitted the same
        way, so an absent description is absent instead of an empty comment."""
        payload = {"title": title, "head": head}
        if base:
            payload["base"] = base
        if body:
            payload["body"] = body
        resp = self._request(
            "POST",
            f"{self._base(base_url)}/repos/{org}/{repo}/pulls",
            token,
            what="open the pull request",
            json=payload,
        )
        return _require_view(resp, viewer_login, what="open the pull request")

    def approve_pull_request(
        self,
        org: str,
        repo: str,
        number: int,
        token: str,
        *,
        viewer_login: str,
        base_url: Optional[str] = None,
    ) -> PullRequestView:
        """Approve someone else's pull request as the linked human.

        The pull request is read first and the refusal is decided locally, so a self-approval
        never reaches the review endpoint and there is no retry under any other credential. The
        answer is a fresh read of the pull request rather than the review object."""
        pr_url = f"{self._base(base_url)}/repos/{org}/{repo}/pulls/{number}"
        current = _require_view(
            self._request("GET", pr_url, token, what="read the pull request"),
            viewer_login,
            what="read the pull request",
        )
        if not current.can_approve:
            if current.approve_blocked_reason == _REASON_SELF:
                raise GitHubPrError(
                    "the pull request cannot be approved by this account", kind="self_approval"
                )
            raise GitHubPrError("the pull request cannot be approved by this account", kind="not_approvable")

        self._request(
            "POST",
            f"{pr_url}/reviews",
            token,
            what="approve the pull request",
            json={"event": "APPROVE"},
        )
        return _require_view(
            self._request("GET", pr_url, token, what="read the pull request"),
            viewer_login,
            what="read the pull request",
        )

    def merge_pull_request(
        self,
        org: str,
        repo: str,
        number: int,
        token: str,
        *,
        viewer_login: str,
        base_url: Optional[str] = None,
    ) -> PullRequestView:
        """Merge a pull request as the linked human.

        No merge method is sent (the repository's configured default is the right one) and no
        ``sha`` either (a stale one turns "the branch moved" into a confusing 409). The answer is
        a fresh read, so a successful merge reports ``state: merged``."""
        pr_url = f"{self._base(base_url)}/repos/{org}/{repo}/pulls/{number}"
        self._request("PUT", f"{pr_url}/merge", token, what="merge the pull request", json={})
        return _require_view(
            self._request("GET", pr_url, token, what="read the pull request"),
            viewer_login,
            what="read the pull request",
        )

    @staticmethod
    def _base(base_url: Optional[str]) -> str:
        return (base_url or GITHUB_DEFAULT_BASE).rstrip("/")

    def _request(self, method: str, url: str, token: str, *, what: str, **kwargs) -> httpx.Response:
        """One request under the caller's token, with the status-to-kind mapping applied. The
        token is a per-request header and is never logged or folded into an exception; the
        client is closed only when this service owns it."""
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        }
        client = self._client or httpx.Client()
        try:
            resp = client.request(method, url, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            logger.exception("[pr] transport failure while trying to %s", what)
            raise GitHubPrError(
                f"could not reach GitHub to {what} ({type(exc).__name__})", kind="provider_error"
            ) from None
        finally:
            if self._owns_client and self._client is None:
                client.close()

        if 200 <= resp.status_code < 300:
            return resp
        raise _error_for(resp, what)


def _error_for(resp: httpx.Response, what: str) -> GitHubPrError:
    """Classify a non-2xx into a kind; the body is read to decide and then dropped.

    403 is the capability signal. 405 and 409 are the merge's two ordinary refusals. 422 needs the
    body to tell apart a review on one's own pull request, a head branch with no commits ahead of
    the base (stated by GitHub in ``errors[].message``, which :func:`_message_of` concatenates),
    a duplicate pull request and everything else."""
    status = resp.status_code
    message = _message_of(resp)

    if status == 403:
        return GitHubPrError(
            f"GitHub declined: this App is not granted pull-request access for the org (HTTP {status})",
            kind="capability_missing",
        )
    if status == 404:
        return GitHubPrError(
            f"GitHub could not find the pull request or repository (HTTP {status})",
            kind="not_found",
        )
    if status == 405:
        return GitHubPrError(f"GitHub declined the merge (HTTP {status})", kind="not_mergeable")
    if status == 409:
        return GitHubPrError(f"GitHub reported a conflicting state (HTTP {status})", kind="conflict")
    if status == 422:
        if "own pull request" in message or "own pull-request" in message:
            return GitHubPrError(
                f"GitHub refused a review by the pull request's author (HTTP {status})",
                kind="self_approval",
            )
        if "no commits between" in message:
            return GitHubPrError(
                f"GitHub found no commits between the two branches (HTTP {status})",
                kind="no_commits",
            )
        if "a pull request already exists" in message:
            return GitHubPrDuplicateError(
                f"GitHub reports a pull request already exists for this head (HTTP {status})",
                kind="conflict",
            )
        return GitHubPrError(f"GitHub declined the request as invalid (HTTP {status})", kind="conflict")
    logger.warning("[pr] GitHub answered HTTP %s while trying to %s", status, what)
    return GitHubPrError(f"GitHub failed to {what} (HTTP {status})", kind="provider_error")


def _project(item, viewer_login: str) -> Optional[PullRequestView]:
    """One provider PR object onto one :class:`PullRequestView`, or ``None`` when unusable.

    A projection, never a pass-through. ``state`` is derived: GitHub's own is only ``open`` or
    ``closed``, and a merged pull request is the interesting row on this surface. ``author`` falls
    back to empty, never to a login: a deleted account leaves ``user: null``. ``mergeable`` is
    only trusted as ``False``: the list endpoint omits the key and a single read answers ``null``
    while computing, and both fold to ``None`` so no consumer can claim "still checking"."""
    if not isinstance(item, dict):
        return None
    number = item.get("number")
    if not isinstance(number, int) or isinstance(number, bool):
        return None

    user = item.get("user")
    author = _text(user.get("login")) if isinstance(user, dict) else ""
    head = item.get("head")
    head_sha = _text(head.get("sha")) if isinstance(head, dict) else ""
    merged = bool(_text(item.get("merged_at"))) or item.get("merged") is True
    state = "merged" if merged else (_text(item.get("state")) or "closed")

    mergeable = item.get("mergeable")
    if not isinstance(mergeable, bool):
        mergeable = None

    can_approve, reason = _approval_standing(state, author, viewer_login)
    return PullRequestView(
        number=number,
        title=_text(item.get("title")),
        state=state,
        author=author,
        head_sha=head_sha,
        url=_text(item.get("html_url")),
        can_approve=can_approve,
        approve_blocked_reason=reason,
        mergeable=mergeable,
        merge_commit_sha=(_text(item.get("merge_commit_sha")) or None) if merged else None,
    )


def _approval_standing(state: str, author: str, viewer_login: str) -> tuple[bool, Optional[str]]:
    """May the linked human approve this pull request, and if not, why: a closed pull request
    takes no review, an unresolvable viewer fails closed, and the author may not review their
    own. A pull request with no author is approvable."""
    if state != "open":
        return False, _REASON_NOT_OPEN
    viewer = viewer_login.strip()
    if not viewer:
        return False, _REASON_NO_VIEWER
    if author and author.lower() == viewer.lower():
        return False, _REASON_SELF
    return True, None


def _require_view(resp: httpx.Response, viewer_login: str, *, what: str) -> PullRequestView:
    """Project a single-PR response, or raise: a 2xx whose body is not a pull request is a
    provider surprise, not a pull request."""
    try:
        body = resp.json()
    except ValueError:
        body = None
    view = _project(body, viewer_login)
    if view is None:
        raise GitHubPrError(
            f"GitHub returned an unusable response while trying to {what}", kind="provider_error"
        )
    return view


def provider_coordinates(
    read_connection: Callable[[str], Any], connection_id: str
) -> tuple[str, Optional[str]]:
    """The org and API base of the repository's connection, read through ``read_connection`` (the
    connection store's get, passed in because this module holds no credential seam). A connection the
    store refuses or no longer has is ``connection_unavailable``: the repository record is fine, but
    the provider cannot be addressed."""
    try:
        connection = read_connection(connection_id)
    except AppError as err:
        raise GitHubPrError(
            f"the repository's connection is unavailable ({err.kind})", kind="connection_unavailable"
        ) from err
    return connection.org, connection.base_url
