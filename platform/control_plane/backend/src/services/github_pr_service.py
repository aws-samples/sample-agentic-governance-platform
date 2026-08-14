"""Pull-request verbs on a GitHub repository, AS THE LINKED HUMAN (E28/T14 — D14+D15).

A thin ``httpx`` client in the shape of :mod:`services.github_repo_service`: a base URL, a
per-request ``Authorization: Bearer {token}`` header, an injectable ``httpx.Client`` (tests back
it with ``httpx.MockTransport``; production builds a real one), and SAFE error messages only.

WHY THIS IS A SEPARATE MODULE, and the invariant that shape enforces
--------------------------------------------------------------------
``github_repo_service`` is AGP acting as ITSELF: every verb there runs under an org-scoped
App-minted token, which is correct for materializing a repo — the platform really is the actor.
A pull request is different. Opening, approving and merging one are acts of a HUMAN, attributed
to a GitHub account, and AGP's job is to carry the human's own authority rather than to
substitute its own.

So this module **takes its token as a parameter and has no way to obtain one.** It imports no
credential seam, mints nothing, and reads no secret store — the caller (the route) resolves the
E27B linked-user token and hands it over. That is not a stylistic choice: it is the mechanism
behind D15. If this module could reach an App-minted token, a human with a broken link would
silently fall back to it, and because AGP's App is never the author of anybody's pull request,
**every self-approval refusal below would be bypassed** — AGP would approve a human's own PR on
their behalf, which is precisely the reviewer-independence the gate exists to provide. A
structural test (``test_the_service_module_cannot_reach_an_app_token``) reads this file's raw
source and fails on any of those imports appearing, because an import added later is exactly the
change a reviewer would otherwise have to catch by eye.

NOTHING IS PERSISTED. GitHub is the system of record for a pull request; a cached AGP copy would
be a second answer to a question that has one authority, and would go stale the moment anyone
pushed. Every verb reads through.

``can_approve`` IS COMPUTED, NOT DISCOVERED (D15)
-------------------------------------------------
GitHub answers ``422`` for a review on your own PR. Discovering the refusal there means the
button was already offered and clicked, so the projection decides it up front from the viewer's
own login — and :meth:`GitHubPrService.approve_pull_request` refuses locally, without a second
attempt under any other credential. The provider's own 422 is still mapped to the same refusal,
because a defence that exists in two places must agree in both.

Logins are compared CASE-INSENSITIVELY: GitHub logins are case-insensitive, so
``Lars-Svensson`` and ``lars-svensson`` are one human, and a case-sensitive compare would offer
a self-approval the provider then refuses.

FAIL CLOSED ON UNKNOWN STANDING. A blank ``viewer_login`` means AGP cannot establish which
account it would be reviewing as — so it refuses. Permitting it would risk exactly the
self-approval this module exists to prevent, and "we could not tell" is not evidence of
independence.

SAFE MESSAGES ONLY. A provider body can echo attacker-influenced content, so no
:class:`GitHubPrError` message ever carries one — only a fixed string plus the status code.
Bodies ARE read, to CLASSIFY a failure into a ``.kind``, and then discarded. Transport failures
surface the exception TYPE only and are re-raised ``from None``, because a chained exception's
args can carry a URL with auth material (the ``github_user_oauth`` rule).
"""

from __future__ import annotations

import logging
from typing import List, Optional

import httpx

from models.repository import PullRequestView

logger = logging.getLogger(__name__)

GITHUB_DEFAULT_BASE = "https://api.github.com"

# How many pull requests one page of the tab shows. The Ops surface is a summary, not a PR
# browser: an operator opens it to see what is outstanding on THIS repo, and a repo with more
# than this many open PRs is a repo whose PRs are read on GitHub. No pagination follow, so the
# tab cannot spend N round-trips on a page nobody scrolls.
_PER_PAGE = 30

# ``state=all`` deliberately, not ``open``. A merged PR is the most interesting row on this
# surface — it is what registered the prod candidate an OWNER then promotes — so hiding closed
# ones would omit the reason production is about to change.
_LIST_STATE = "all"

# The refusal reasons. FIXED sentences: they are rendered verbatim to an operator beside a
# suppressed button, so each states the cause and offers no remedy it cannot deliver.
#
# NOTE none of the non-self reasons may contain the substring "own" (a test asserts it), because
# that is how the UI and the tests tell "this is YOUR pull request" apart from every other
# reason an approval is unavailable. It rules out the word "unknown" here, which contains it.
_REASON_SELF = "You opened this pull request — GitHub does not accept your own review."
_REASON_NOT_OPEN = "This pull request is no longer open, so it accepts no further review."
_REASON_NO_VIEWER = (
    "AGP could not establish which GitHub account it would review as. Link your GitHub "
    "account to review as yourself."
)


class GitHubPrError(Exception):
    """A pull-request operation failed. Carries a SAFE message (never the token, never the
    provider body) plus a ``.kind`` the route maps to a FIXED HTTP detail.

    The kinds, and why each is distinct rather than collapsed:

    ``capability_missing``
        The org's App is not granted ``pull_requests``. This is a MANUAL per-org grant and
        GitHub does not retro-apply a manifest permission change to an already-created App, so
        it is an ordinary and expected state for an org onboarded before this feature — the
        frontend resolves it to a HIDDEN tab. It must never be a 500, because a 500 renders a
        BROKEN tab, which is worse than no tab.
    ``self_approval``
        D15's refusal. Its own kind so the UI can state the actual reason rather than "failed".
    ``not_approvable``
        A review cannot be taken for a reason that is NOT the caller being the author — the PR
        is closed, or AGP cannot establish whose account it would act as. Kept apart from
        ``self_approval`` so the two never wear each other's copy.
    ``not_mergeable``
        The provider refuses the merge (checks, protection, an unmergeable state). A state, not
        a fault.
    ``conflict``
        The request cannot be satisfied as asked — a duplicate PR, a stale head. Also a state.
    ``no_commits``
        There is nothing to open a pull request FOR: the head branch is not ahead of the base.
        Its own kind because it is the 422 an operator meets by accident, and because the
        ``conflict`` copy stated two causes this is not — a duplicate pull request and a moved
        branch — which is a surface asserting what it never established. Collapsing it back
        would restore that.
    ``not_found``
        The repository or PR is not visible to this token.
    ``provider_error``
        Anything else, including a transport failure. The only kind that means "AGP does not
        know what happened".
    """

    # ``kind`` is KEYWORD-ONLY on purpose. The test guard that compares the kinds this module can
    # produce against the route's ``_PR_ERROR`` table reads the AST for ``kind=`` keywords, the
    # default below, and locals named ``kind`` — it cannot see a positional second argument. With
    # a positional-capable signature, ``GitHubPrError("x", "new_kind")`` is ordinary Python that
    # slips past the guard and ships an unmapped kind as a 502 "GitHub request failed" on a
    # request GitHub answered fine. Forbidding the form makes the guard exhaustive by
    # construction instead of by convention.
    def __init__(self, message: str, *, kind: str = "provider_error") -> None:
        super().__init__(message)
        self.kind = kind


def _message_of(resp: httpx.Response) -> str:
    """GitHub's ``message`` (+ any ``errors[].message``), for CLASSIFICATION ONLY.

    Read to decide a ``.kind`` and then DISCARDED — it is never folded into a
    :class:`GitHubPrError` message, because a provider body can echo attacker-influenced
    content and this epic has already had to truncate a field that carried an AWS account id.
    Lowercased here because every caller matches substrings against it."""
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
    """Pull-request verbs under a CALLER-SUPPLIED user token. ``client`` is injectable."""

    def __init__(self, *, client: Optional[httpx.Client] = None) -> None:
        # A caller-supplied client carries NO pre-set auth — auth is per-request, so one client
        # can never leak one human's token onto another's call.
        self._client = client
        self._owns_client = client is None

    # ------------------------------------------------------------------ #
    # The verbs
    # ------------------------------------------------------------------ #

    def list_pull_requests(
        self,
        org: str,
        repo: str,
        token: str,
        *,
        viewer_login: str,
        base_url: Optional[str] = None,
    ) -> List[PullRequestView]:
        """The repo's pull requests, projected onto :class:`PullRequestView`.

        A ``403`` here is THE capability signal: it is what an org whose App lacks
        ``pull_requests`` answers, and the frontend resolves that one kind to a hidden tab.

        A malformed ROW is skipped rather than fatal. One unusable entry in a provider list must
        not blank a tab that could still show the readable ones — and a non-list body answers
        ``[]``, because "the provider said something unexpected" is not a list of pull requests
        but is also not a reason to 500 a page."""
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

        ``base`` is OMITTED ENTIRELY when the caller named none, rather than defaulted: a
        tenant's branch set is open (D8), so "no base" means "the repository's own default
        branch" — a fact the PROVIDER holds. Writing a literal here would be the same hardcode
        the design forbids one layer up. ``body`` is omitted the same way, so an absent
        description is absent instead of an empty comment."""
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
        """Approve someone ELSE's pull request as the linked human (D15).

        THE ORDER IS THE POINT. The PR is READ first and the refusal is decided locally, so a
        self-approval never reaches the review endpoint — and there is NO retry under any other
        credential, because this module holds none to retry with. That is what makes "refused
        with a reason and no App-token retry" observable in a test: the review POST simply does
        not happen.

        The provider's own 422 is mapped to the SAME refusal. Two defences against one mistake
        must agree, or the weaker one becomes a second opinion.

        The answer is a FRESH read of the PR rather than the review object, so the caller sees
        the pull request as it now is instead of an optimistic local edit."""
        pr_url = f"{self._base(base_url)}/repos/{org}/{repo}/pulls/{number}"
        current = _require_view(
            self._request("GET", pr_url, token, what="read the pull request"),
            viewer_login,
            what="read the pull request",
        )
        if not current.can_approve:
            # Refused HERE, before any write. The reason travels with the refusal so the route
            # need not re-derive it, and the kind keeps D15's case distinct from every other.
            kind = "self_approval" if current.approve_blocked_reason == _REASON_SELF else "not_approvable"
            raise GitHubPrError("the pull request cannot be approved by this account", kind=kind)

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

        NO merge METHOD is sent: the repository's own configured default is the right one, and
        naming one here would override a repo setting an org deliberately chose. NO ``sha``
        either — sending a stale one turns "the branch moved" into a confusing 409, while
        omitting it lets the provider merge what is actually there.

        The answer is a FRESH read, so a successful merge reports ``state: merged`` rather than
        the ``merged: true`` acknowledgement, which is not a pull request."""
        pr_url = f"{self._base(base_url)}/repos/{org}/{repo}/pulls/{number}"
        self._request("PUT", f"{pr_url}/merge", token, what="merge the pull request", json={})
        return _require_view(
            self._request("GET", pr_url, token, what="read the pull request"),
            viewer_login,
            what="read the pull request",
        )

    # ------------------------------------------------------------------ #
    # Transport
    # ------------------------------------------------------------------ #

    @staticmethod
    def _base(base_url: Optional[str]) -> str:
        return (base_url or GITHUB_DEFAULT_BASE).rstrip("/")

    def _request(self, method: str, url: str, token: str, *, what: str, **kwargs) -> httpx.Response:
        """One request under the caller's token, with the status→kind mapping applied.

        The token is a per-request header and is NEVER logged or folded into an exception. The
        client is closed only when this service owns it, the ``github_repo_service`` idiom."""
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        }
        client = self._client or httpx.Client()
        try:
            resp = client.request(method, url, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            # Never surface the exception VALUE — a URL in it can carry auth material. Type only,
            # and ``from None`` so the chained original cannot leak it either.
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


# --------------------------------------------------------------------------- #
# The status → kind mapping
# --------------------------------------------------------------------------- #


def _error_for(resp: httpx.Response, what: str) -> GitHubPrError:
    """Classify a non-2xx into a ``.kind``. The body is read to DECIDE and then dropped.

    ``403`` is the capability signal and is the reason this mapping exists at all: GitHub
    answers it when the App is not granted ``pull_requests``, and that state must be
    distinguishable from every other failure so the frontend can hide the tab instead of
    rendering a broken one.

    ``405`` and ``409`` are the merge's two ordinary refusals — the provider will not merge, or
    the branch moved. Neither is a fault.

    ``422`` is a validation refusal and needs the body to tell three very different things
    apart: a review on one's own pull request (D15's refusal), a head branch with no commits
    ahead of the base, and everything else (a duplicate PR, a rejected field). Only the
    CLASSIFICATION crosses; the text does not — which is exactly why the no-commits case needs
    its own kind rather than the generic bucket's copy."""
    status = resp.status_code
    message = _message_of(resp)

    if status == 403:
        return GitHubPrError(
            "GitHub declined: this App is not granted pull-request access for the org "
            f"(HTTP {status})",
            kind="capability_missing",
        )
    if status == 404:
        return GitHubPrError(
            f"GitHub could not find the pull request or repository (HTTP {status})",
            kind="not_found",
        )
    if status == 405:
        return GitHubPrError(
            f"GitHub declined the merge (HTTP {status})", kind="not_mergeable"
        )
    if status == 409:
        return GitHubPrError(
            f"GitHub reported a conflicting state (HTTP {status})", kind="conflict"
        )
    if status == 422:
        # A self-review refusal reaches here only if the local check was bypassed (a login AGP
        # could not resolve). It must still read as the SAME refusal, never as a server fault.
        if "own pull request" in message or "own pull-request" in message:
            return GitHubPrError(
                f"GitHub refused a review by the pull request's author (HTTP {status})",
                kind="self_approval",
            )
        # "No commits between main and dev" — the ONE 422 an operator meets by accident, and the
        # generic fallback below described it as two things it is not (a duplicate PR, a moved
        # branch). GitHub states it in ``errors[].message`` rather than ``message``, which
        # ``_message_of`` already concatenates and lowercases. Matched BEFORE the fallback,
        # because the fallback matches everything.
        if "no commits between" in message:
            return GitHubPrError(
                f"GitHub found no commits between the two branches (HTTP {status})",
                kind="no_commits",
            )
        return GitHubPrError(
            f"GitHub declined the request as invalid (HTTP {status})", kind="conflict"
        )
    logger.warning("[pr] GitHub answered HTTP %s while trying to %s", status, what)
    return GitHubPrError(
        f"GitHub failed to {what} (HTTP {status})", kind="provider_error"
    )


# --------------------------------------------------------------------------- #
# The projection
# --------------------------------------------------------------------------- #


def _project(item, viewer_login: str) -> Optional[PullRequestView]:
    """One provider PR object → one :class:`PullRequestView`, or ``None`` when unusable.

    A PROJECTION, never a pass-through: only the fields the Ops surface renders cross, so a
    provider body cannot reach a client through this.

    ``state`` IS DERIVED. GitHub's own ``state`` is only ``open``/``closed`` — a merged PR is
    ``closed`` there — and on this surface the merged case is the interesting one, because it is
    what registered the prod candidate an OWNER then promotes. Reporting a shipped PR as merely
    "closed" would lose the one distinction that matters.

    ``author`` falls back to EMPTY, never to a login. A deleted GitHub account leaves
    ``user: null``; defaulting it to the viewer would suppress an approve on a PR they did not
    write, and defaulting it to anything else would assert an identity nobody established.

    ``mergeable`` IS ONLY TRUSTED AS ``False``. It stays three-valued, but note what the two
    non-false values do NOT distinguish. The LIST endpoint omits the key altogether — verified
    against live GitHub: ``GET /repos/{o}/{r}/pulls`` sends no ``mergeable``, while
    ``GET /pulls/{n}`` answers ``mergeable: true`` for the same pull request — and a single-PR read
    answers ``null`` while its async computation is in flight. The fold below turns both into
    ``None`` deliberately, so no consumer can claim "the provider is still checking": that is
    indistinguishable here from "nobody ever asked", and a tab that asserted it said so on every
    row forever (E28 finding #7). Absent must nonetheless never become ``False``, because that is
    the reading that suppresses a merge which would have succeeded."""
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
        # Anything that is not a real boolean is "nothing was established" — which is the honest
        # reading of GitHub's ``null`` AND of a body that omitted the key, and deliberately does
        # not tell the two apart. See the docstring: a consumer that distinguished them would be
        # distinguishing something this value does not carry.
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
    )


def _approval_standing(state: str, author: str, viewer_login: str) -> "tuple[bool, Optional[str]]":
    """May the linked human approve this pull request, and if not, WHY (D15)?

    Three refusals, in this order, each with its own sentence:

    1. **Not open.** GitHub takes no review on a closed or merged PR, so an approve button there
       is an affordance whose every click is refused.
    2. **No resolvable viewer.** AGP cannot say which account it would review as, so it refuses
       — the fail-closed direction, since permitting it risks exactly the self-approval below.
    3. **The viewer IS the author.** D15's refusal, compared case-insensitively because GitHub
       logins are.

    A PR with NO author is approvable: there is nobody for the viewer to be, so nothing
    establishes a self-approval. Refusing it would suppress a legitimate review over a deleted
    account."""
    if state != "open":
        return False, _REASON_NOT_OPEN
    viewer = viewer_login.strip()
    if not viewer:
        return False, _REASON_NO_VIEWER
    if author and author.lower() == viewer.lower():
        return False, _REASON_SELF
    return True, None


def _require_view(resp: httpx.Response, viewer_login: str, *, what: str) -> PullRequestView:
    """Project a single-PR response, or raise. A 2xx whose body is not a pull request is a
    provider surprise, not a pull request — and answering a fabricated one would be worse than
    saying so."""
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
