"""GitHub org WRITE client for E20/T6 template rollout.

A thin ``httpx`` client that mirrors ``connection_verify``'s shape — a base URL,
``Authorization: Bearer {token}`` set on EACH request, and an injectable
``httpx.Client`` (tests back it with ``httpx.MockTransport`` or a Mock; the service
builds a real one). Unlike ``connection_verify`` (read-only probes) this owns the
WRITE sequence: create a repo in the org, then push the scaffold contents as a
single initial commit via the GitHub git-data API.

SECURITY: the PAT is used only as a request auth header. It is NEVER logged and
never folded into an exception message.

Push sequence (git-data API, no local git needed):
  1. POST /orgs/{org}/repos            — create the repo with ``auto_init=true``.
     (auto_init gives the repo an initial commit so its git database EXISTS — the
     git-data API 409s "Git Repository is empty" against a truly empty repo.)
  2. GET  /repos/{owner}/{repo}/git/ref/heads/{branch} — the auto-init commit sha (the parent).
  3. POST /repos/{owner}/{repo}/git/blobs   — one base64 blob per scaffold file.
  4. POST /repos/{owner}/{repo}/git/trees   — a tree binding the blobs to paths (scaffold only).
  5. POST /repos/{owner}/{repo}/git/commits — a commit whose parent is the auto-init commit.
  6. PATCH /repos/{owner}/{repo}/git/refs/heads/{branch} — fast-forward the branch (force) to it.

E28B/T1 — this module is now also the GITHUB ADAPTER for the portable
:class:`services.repo_provider.RepoProvider` seam. The five portable verbs (``create_repo``,
``commit_files``, ``set_ci_vars``, ``ensure_pipeline``, ``read_pr``) live in their own section
below. See :mod:`services.repo_provider` for why the interface has five methods and why
``ensure_pipeline`` returning ``None`` is SUCCESS.

E28B/T7 — the GitHub-SHAPED predecessors of those verbs are GONE, not deprecated:
``generate_from_template`` (+ its async-copy readiness wait), ``create_branch`` (+ the
default-branch re-assert), ``commit_file``, ``create_environment``,
``set_environment_variables``, ``set_repo_variables``, ``list_template_repos`` and
``set_repo_metadata``. Each had zero call sites once T2/T3/T4 landed. They are not kept for
"compatibility": every one of them was a SECOND write to a repository the provider was already
writing to, and keeping a callable path back to that shape is how the race this epic deleted
would return. What remains here is the zip-scaffold rollout path (``create_repo_from_zip``,
still used for template + infra-repo pushes), the delete/probe pair, and the five portable verbs.
"""

from __future__ import annotations

import base64
import binascii
import io
import logging
import zipfile
from typing import Optional

import httpx

from models.repository import PullRequestView

# E28B/T1 — the PRIVATE name is imported DELIBERATELY. ``github_pr_service._project`` is the
# single provider-PR → :class:`PullRequestView` projection in the backend, and it carries
# hard-won semantics a second copy would drift from within one epic: ``state`` derived so a
# merged PR reads "merged" rather than GitHub's "closed", ``mergeable`` folded to three values
# where absent is never False, an author that falls back to empty rather than to a login. A
# duplicate here would be a second answer to a question that has one authority. Nothing is
# borrowed in the other direction: ``github_pr_service`` must not import from this module (its
# ``test_the_service_module_cannot_reach_an_app_token`` guard is what keeps a human's pull
# request from being approved under AGP's own App token), and this import does not weaken that
# guard — a pure projection function holds no credential seam.
from services.github_pr_service import _project as _project_pull_request

# E28C/T1 — the seam's own projection type, imported so ``read_repo`` returns the SAME class the
# Protocol declares. Only a frozen dataclass crosses; :mod:`services.repo_provider` holds no
# transport and no credential (its own guard test asserts that), so this direction of the
# dependency stays adapter → contract, never the reverse.
from services.repo_provider import RepoView

logger = logging.getLogger(__name__)

GITHUB_DEFAULT_BASE = "https://api.github.com"

# The branch ``create_repo_from_zip`` pushes its scaffold onto. This is the branch GitHub's
# ``auto_init`` seeds, and that path pushes TEMPLATE/INFRA repos (not agent repos), whose trunk
# is not project config — an agent repo's trunk comes from ``Project.trunk_branch`` (D-B5) and is
# never read from here.
_DEFAULT_BRANCH = "main"
_INITIAL_COMMIT_MESSAGE = "Initial commit from AGP agent template"

# E28C/T1 — ``list_repos`` pagination. GitHub's own maximum ``per_page`` is 100, so the cap is a
# ceiling on PAGES, and reaching it RAISES rather than truncating (see the method's docstring):
# 20 pages is 2,000 repositories, far past any org this surface is designed for, so hitting it is
# a pagination bug or an org that needs a different design — either way something to say out loud.
_LIST_REPOS_PER_PAGE = 100
_LIST_REPOS_MAX_PAGES = 20


class GitHubRepoError(Exception):
    """A GitHub write failed. Carries a SAFE message (never the token/response body)."""


class GitHubRepoService:
    """Thin GitHub write client. ``client`` is injectable for tests."""

    def __init__(self, *, client: Optional[httpx.Client] = None) -> None:
        # A caller-supplied client carries NO pre-set auth — auth is per-request.
        self._client = client
        self._owns_client = client is None

    # ------------------------------------------------------------------ #

    def create_repo_from_zip(
        self,
        org: str,
        repo_name: str,
        zip_bytes: bytes,
        token: str,
        base_url: Optional[str] = None,
    ) -> str:
        """Create ``repo_name`` under ``org`` and push the zip's contents as the
        initial commit. Returns the repo's ``html_url``.

        Raises ``GitHubRepoError`` on any API failure (message is SAFE — no token,
        no response body)."""
        base = (base_url or GITHUB_DEFAULT_BASE).rstrip("/")
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        }

        client = self._client or httpx.Client()
        try:
            html_url, owner = self._create_repo(client, base, headers, org, repo_name)
            files = _read_zip(zip_bytes)
            if files:
                self._push_initial_commit(client, base, headers, owner, repo_name, files)
            return html_url
        except httpx.HTTPError as exc:
            # Never surface the exception value (URLs can carry query auth) — type only.
            logger.exception("[template-rollout] GitHub write failed for %s/%s", org, repo_name)
            raise GitHubRepoError(
                f"GitHub request failed while creating '{org}/{repo_name}': {type(exc).__name__}"
            ) from None
        finally:
            if self._owns_client and self._client is None:
                client.close()

    def delete_repo(
        self,
        org: str,
        repo: str,
        token: str,
        base_url: Optional[str] = None,
    ) -> None:
        """Delete ``org/repo`` (E22 template-catalog ``delete_template`` backing call).

        DELETE ``/repos/{org}/{repo}``. GitHub returns 204 on success. A 404 (the repo is
        already gone) is treated as success, so the delete is idempotent. Any other non-2xx
        raises ``GitHubRepoError``.

        Uses the same per-request Bearer auth as :meth:`create_repo_from_zip`; the token
        is NEVER logged or folded into an exception message. Raises ``GitHubRepoError``
        on any API failure (message is SAFE — no token, no response body)."""
        base = (base_url or GITHUB_DEFAULT_BASE).rstrip("/")
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        }
        client = self._client or httpx.Client()
        try:
            resp = client.delete(f"{base}/repos/{org}/{repo}", headers=headers)
            # 204 = deleted; 404 = already gone -> idempotent success. Anything else fails.
            if resp.status_code not in (200, 202, 204, 404):
                raise GitHubRepoError(
                    f"failed to delete repo '{org}/{repo}' (HTTP {resp.status_code}: "
                    f"{_github_message(resp)})"
                )
        except httpx.HTTPError as exc:
            logger.exception("[template-catalog] GitHub delete-repo failed for %s/%s", org, repo)
            raise GitHubRepoError(
                f"GitHub request failed while deleting '{org}/{repo}': {type(exc).__name__}"
            ) from None
        finally:
            if self._owns_client and self._client is None:
                client.close()

    def repo_exists(
        self,
        org: str,
        repo: str,
        token: str,
        base_url: Optional[str] = None,
    ) -> bool:
        """Does ``org/repo`` still exist? (E23/T11 delete-preview reachability probe).

        GET ``/repos/{org}/{repo}``: 200 → True (present), 404 → False (gone). Any other
        non-2xx raises ``GitHubRepoError`` (the caller maps a raise to state ``unknown``).
        Mirrors :meth:`delete_repo`'s idiom — READ-ONLY (deletes nothing).

        E28C/T1 — the status reading is now :meth:`_read_repo_body`'s, SHARED with
        :meth:`read_repo`, which needs the same endpoint's body. One authority for "404 is
        absence, a 403 is not": two copies of that rule is how the existence probe and the
        reconcile projection would come to disagree about whether an expired token means a
        customer's repository is gone.

        Uses the same per-request Bearer auth as :meth:`create_repo_from_zip`; the token
        is NEVER logged or folded into an exception message. Raises ``GitHubRepoError``
        on any API failure (message is SAFE — no token, no response body)."""
        base = (base_url or GITHUB_DEFAULT_BASE).rstrip("/")
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        }
        client = self._client or httpx.Client()
        try:
            return self._read_repo_body(client, base, headers, org, repo) is not None
        except httpx.HTTPError as exc:
            logger.exception("[delete-preview] GitHub repo-exists probe failed for %s/%s", org, repo)
            raise GitHubRepoError(
                f"GitHub request failed while probing '{org}/{repo}': {type(exc).__name__}"
            ) from None
        finally:
            if self._owns_client and self._client is None:
                client.close()

    # ------------------------------------------------------------------ #
    # E28B/T1 — the RepoProvider seam (D-B1)
    #
    # The five portable verbs, ADDED ALONGSIDE the methods above rather than replacing
    # them: their call sites still exist and a method deleted before its caller is a
    # broken intermediate state. A later task removes the ones that go dead.
    #
    # Every method here keeps this module's established idiom: per-request Bearer auth, a
    # token that is NEVER logged and never folded into an exception, and ``GitHubRepoError``
    # with a SAFE message (no token, no raw response body).
    # ------------------------------------------------------------------ #

    def create_repo(
        self,
        org: str,
        name: str,
        *,
        private: bool,
        token: str,
        base_url: Optional[str] = None,
    ) -> str:
        """``RepoProvider.create_repo`` — make ``org/name`` exist; return its URL.

        ``auto_init=True`` is NOT cosmetic: it seeds one commit so the repository's git
        database EXISTS. Against a truly empty repo the git-data API — which
        :meth:`commit_files` is built on — answers 409 "Git Repository is empty", so a repo
        created without it cannot receive the very next call in the materialize sequence.
        The seeded README does not survive: :meth:`commit_files` builds its tree with no
        ``base_tree``, so the first push replaces the whole tree.

        Idempotent, by the same narrow rule :meth:`create_branch` and
        :meth:`generate_from_template` already use: a 422 whose detail says the name
        already exists is a benign RE-RUN (a retried materialize must converge, and
        ``retry_materialize`` re-enters here), so the URL is read back instead of raised
        over. EVERY other 422 still fails closed — 422 is overloaded on this endpoint and
        treating all of them as benign would swallow a rejected name or a quota refusal.
        """
        base = (base_url or GITHUB_DEFAULT_BASE).rstrip("/")
        headers = self._headers(token)
        client = self._client or httpx.Client()
        try:
            resp = client.post(
                f"{base}/orgs/{org}/repos",
                headers=headers,
                json={"name": name, "private": private, "auto_init": True},
            )
            if resp.status_code in (200, 201):
                body = resp.json()
                return body.get("html_url") or f"{base}/{org}/{name}"

            detail = _github_message(resp)
            if not (resp.status_code == 422 and "already exists" in detail.lower()):
                raise GitHubRepoError(
                    f"could not create repo '{org}/{name}' (HTTP {resp.status_code}: {detail})"
                )
            # The 422 body describes the REJECTION, not the repo — it carries no html_url.
            # Read it back, because the caller PERSISTS the returned url.
            logger.info(
                "[repo-provider] %s/%s already exists — treating the 422 as a benign re-run",
                org,
                name,
            )
            existing = client.get(f"{base}/repos/{org}/{name}", headers=headers)
            existing_body = existing.json() if existing.status_code == 200 else {}
            if not isinstance(existing_body, dict):
                existing_body = {}
            return existing_body.get("html_url") or f"{base}/{org}/{name}"
        except httpx.HTTPError as exc:
            logger.exception("[repo-provider] GitHub create-repo failed for %s/%s", org, name)
            raise GitHubRepoError(
                f"GitHub request failed while creating '{org}/{name}': {type(exc).__name__}"
            ) from None
        finally:
            if self._owns_client and self._client is None:
                client.close()

    def commit_files(
        self,
        org: str,
        repo: str,
        files: "dict[str, bytes]",
        *,
        branch: str,
        message: str,
        token: str,
        base_url: Optional[str] = None,
    ) -> str:
        """``RepoProvider.commit_files`` — put ``files`` on ``branch`` in ONE commit; return
        the commit sha.

        Internally blobs→tree→commit→ref, four GitHub calls the caller must never see. ONE
        commit is the whole point: it means ONE CI trigger, so a single ``[skip ci]`` in
        ``message`` suppresses the materialize build with no branch filters to keep correct,
        and it means there is no second write left to race the first (the four E28 findings
        that came from a six-write materialize).

        The tree is built with NO ``base_tree``, so the branch ends up carrying EXACTLY
        ``files``. Nothing pre-existing is carried forward and no path is inspected — a
        template author can move ``build.yml``, restructure directories or add a workflow
        and it flows through untouched.

        IDEMPOTENT BY CONTENT, which is what makes a retried materialize safe. Git objects
        are content-addressed, so re-posting identical blobs and an identical tree yields
        the SAME tree sha. When that sha already matches the branch tip's tree, this writes
        no commit and does not move the ref: the branch is byte-identical, so there is
        nothing to commit and — decisively — no push event, hence no duplicate build. The
        existing tip sha is returned. (The blob/tree POSTs do create unreferenced git
        objects, which is not a change to the repository as any observer sees it; git
        collects them.)

        A branch that does not exist yet is CREATED here rather than refused: the commit is
        parented on nothing (a root commit) and the ref is POSTed. That keeps trunk naming
        the project's business (D-B2) instead of requiring the trunk to be whatever GitHub's
        ``auto_init`` happened to name.

        ``files`` must be non-empty. An empty mapping would build an EMPTY tree — i.e.
        delete every file on the branch — which is never what a caller with nothing to push
        meant, so it fails closed instead.
        """
        if not files:
            raise GitHubRepoError(
                f"refusing to commit an empty file set to '{org}/{repo}' (branch '{branch}') "
                f"— an empty tree would delete the branch's entire contents"
            )
        base = (base_url or GITHUB_DEFAULT_BASE).rstrip("/")
        headers = self._headers(token)
        repo_base = f"{base}/repos/{org}/{repo}"
        client = self._client or httpx.Client()
        try:
            # 1) The branch's current tip + the tree it points at (None ⇒ the branch is new).
            parent_sha, parent_tree_sha = self._read_branch_tip(
                client, repo_base, headers, org, repo, branch
            )

            # 2) One blob per file. Content-addressed: identical content ⇒ identical sha.
            tree_items = []
            for path, content in files.items():
                blob_resp = client.post(
                    f"{repo_base}/git/blobs",
                    headers=headers,
                    json={
                        "content": base64.b64encode(content).decode("ascii"),
                        "encoding": "base64",
                    },
                )
                self._require_ok(blob_resp, f"create blob for '{path}' on '{org}/{repo}'")
                tree_items.append(
                    {
                        "path": path,
                        "mode": "100644",
                        "type": "blob",
                        "sha": self._sha_of(
                            blob_resp, f"the blob for '{path}' on '{org}/{repo}'"
                        ),
                    }
                )

            # 3) The tree. No base_tree — the commit's tree is EXACTLY ``files``.
            tree_resp = client.post(
                f"{repo_base}/git/trees", headers=headers, json={"tree": tree_items}
            )
            self._require_ok(tree_resp, f"create tree on '{org}/{repo}'")
            tree_sha = self._sha_of(tree_resp, f"the tree on '{org}/{repo}'")

            # 4) THE IDEMPOTENCE GATE. Same content ⇒ same tree sha ⇒ the branch already
            # carries this exact tree. Writing a commit here would be an empty commit AND a
            # push event (a build for nothing), so return the tip untouched instead.
            if parent_tree_sha is not None and parent_tree_sha == tree_sha:
                logger.info(
                    "[repo-provider] %s/%s branch '%s' already carries this tree — no commit",
                    org,
                    repo,
                    branch,
                )
                return parent_sha

            # 5) The single commit. ``parents: []`` on a branch nobody cut yet (a root commit).
            commit_resp = client.post(
                f"{repo_base}/git/commits",
                headers=headers,
                json={
                    "message": message,
                    "tree": tree_sha,
                    "parents": [parent_sha] if parent_sha else [],
                },
            )
            self._require_ok(commit_resp, f"create commit on '{org}/{repo}'")
            commit_sha = self._sha_of(commit_resp, f"the commit on '{org}/{repo}'")

            # 6) Point the ref at it — PATCH an existing branch, POST a new one.
            if parent_sha:
                # ``force`` because the tree is built from scratch rather than from the tip,
                # so the new commit need not be a fast-forward of what was there.
                ref_resp = client.patch(
                    f"{repo_base}/git/refs/heads/{branch}",
                    headers=headers,
                    json={"sha": commit_sha, "force": True},
                )
            else:
                ref_resp = client.post(
                    f"{repo_base}/git/refs",
                    headers=headers,
                    json={"ref": f"refs/heads/{branch}", "sha": commit_sha},
                )
            self._require_ok(ref_resp, f"update branch '{branch}' on '{org}/{repo}'")
            return commit_sha
        except httpx.HTTPError as exc:
            logger.exception("[repo-provider] GitHub commit-files failed for %s/%s", org, repo)
            raise GitHubRepoError(
                f"GitHub request failed while committing {len(files)} file(s) to "
                f"'{org}/{repo}' (branch '{branch}'): {type(exc).__name__}"
            ) from None
        finally:
            if self._owns_client and self._client is None:
                client.close()

    # ------------------------------------------------------------------ #
    # E28B/T3 — adopting the trunk the PROJECT named (D-B5)
    #
    # These two exist because ``create_repo`` passes ``auto_init=True``, which is a PRECONDITION
    # of ``commit_files`` (the git-data API answers 409 "Git Repository is empty" against a
    # truly empty repo) — not a nicety that could simply be dropped. Auto-init creates a branch
    # whose name comes from the ORG's default-branch setting, which AGP does not control.
    #
    # So a project whose trunk is not that name ended up with TWO branches: the auto-init one
    # (still ``default_branch``, so PRs open against it) and the trunk carrying the template.
    # ``build.yml``'s ``branches:`` filter then never fired on the branch that had the code —
    # a repo that looks materialized and never builds. Re-pointing HEAD and reclaiming the
    # stray ref is what makes "the trunk is project config" true rather than merely accepted.
    #
    # ORDER IS LOAD-BEARING: GitHub REFUSES to delete the default branch, so the default must
    # move FIRST. Both are no-ops when the trunk is already what auto-init produced, which is
    # the overwhelmingly common case.
    # ------------------------------------------------------------------ #

    def set_default_branch(
        self,
        org: str,
        repo: str,
        branch: str,
        *,
        token: str,
        base_url: Optional[str] = None,
    ) -> None:
        """Point ``org/repo``'s ``default_branch`` at ``branch``.

        The default branch is what PRs open against and what a provider serves as HEAD, so a
        repository whose template lives on a non-default branch is one nobody can review or build
        normally. Fails closed: a default that did not move is not cosmetic.
        """
        base = (base_url or GITHUB_DEFAULT_BASE).rstrip("/")
        client = self._client or httpx.Client()
        try:
            resp = client.patch(
                f"{base}/repos/{org}/{repo}",
                headers=self._headers(token),
                json={"default_branch": branch},
            )
            self._require_ok(resp, f"set default branch of '{org}/{repo}' to '{branch}'")
        except httpx.HTTPError as exc:
            logger.exception("[repo-provider] GitHub set-default-branch failed for %s/%s", org, repo)
            raise GitHubRepoError(
                f"GitHub request failed while setting the default branch of '{org}/{repo}' to "
                f"'{branch}': {type(exc).__name__}"
            ) from None
        finally:
            if self._owns_client and self._client is None:
                client.close()

    def delete_branch(
        self,
        org: str,
        repo: str,
        branch: str,
        *,
        token: str,
        base_url: Optional[str] = None,
    ) -> None:
        """Delete ``branch`` on ``org/repo``. An ALREADY-ABSENT branch is success.

        Idempotent by the same narrow rule the rest of this module uses (a retried materialize
        must converge): 404/422 means the ref is not there, which is the state the caller asked
        for. Every other status still fails closed — "AGP could not delete it" is not evidence
        that it is gone, and a stray branch that Actions watches is exactly what this reclaims.
        """
        base = (base_url or GITHUB_DEFAULT_BASE).rstrip("/")
        client = self._client or httpx.Client()
        try:
            resp = client.delete(
                f"{base}/repos/{org}/{repo}/git/refs/heads/{branch}",
                headers=self._headers(token),
            )
            if resp.status_code in (204, 200, 404, 422):
                return
            raise GitHubRepoError(
                f"failed to delete branch '{branch}' on '{org}/{repo}' "
                f"(HTTP {resp.status_code}: {_github_message(resp)})"
            )
        except httpx.HTTPError as exc:
            logger.exception("[repo-provider] GitHub delete-branch failed for %s/%s", org, repo)
            raise GitHubRepoError(
                f"GitHub request failed while deleting branch '{branch}' on '{org}/{repo}': "
                f"{type(exc).__name__}"
            ) from None
        finally:
            if self._owns_client and self._client is None:
                client.close()

    @staticmethod
    def _sha_of(resp, what: str) -> str:
        """The ``sha`` from a git-data 2xx body, or ``GitHubRepoError``.

        A bare ``resp.json()["sha"]`` raises ``KeyError`` on a 2xx that carries no ``sha`` — and
        a ``KeyError`` ESCAPES this file's error contract: every caller of this service catches
        ``GitHubRepoError``, so the materialize step wrapper that would have recorded a failed
        step instead sees an exception type it does not handle. The status check
        (:meth:`_require_ok`) cannot cover this: the response is a 200, and it is the BODY that
        is unusable.

        Not defensive noise — the same discipline :meth:`_read_branch_tip` already applies to
        the ref and commit reads four lines below. A 2xx whose body is not the shape GitHub
        documents is a provider surprise, and saying so is strictly better than a raw
        ``KeyError`` (or worse, proceeding with a ``None`` sha and writing a ref that points at
        nothing).
        """
        try:
            body = resp.json()
        except ValueError:
            body = None
        sha = body.get("sha") if isinstance(body, dict) else None
        if not isinstance(sha, str) or not sha:
            raise GitHubRepoError(f"GitHub named no sha for {what}")
        return sha

    def _read_branch_tip(self, client, repo_base, headers, org, repo, branch):
        """``(commit_sha, tree_sha)`` for ``branch``, or ``(None, None)`` when it does not
        exist.

        Two calls, because the ref only names a COMMIT and the idempotence gate needs that
        commit's TREE. A 404 on the ref is "the branch is new" — the one status that must
        not be an error, since :meth:`commit_files` creates the branch in that case. Any
        other non-2xx still raises: "AGP could not read the tip" is not evidence that the
        branch is absent, and mistaking one for the other would force-create a root commit
        over a branch that already has history.
        """
        ref_get = client.get(f"{repo_base}/git/ref/heads/{branch}", headers=headers)
        if ref_get.status_code == 404:
            return None, None
        self._require_ok(ref_get, f"read branch '{branch}' on '{org}/{repo}'")
        commit_sha = (ref_get.json().get("object") or {}).get("sha")
        if not commit_sha:
            raise GitHubRepoError(
                f"GitHub named no commit for branch '{branch}' on '{org}/{repo}'"
            )
        commit_get = client.get(f"{repo_base}/git/commits/{commit_sha}", headers=headers)
        self._require_ok(commit_get, f"read the tip commit of '{branch}' on '{org}/{repo}'")
        tree_sha = (commit_get.json().get("tree") or {}).get("sha")
        if not tree_sha:
            raise GitHubRepoError(
                f"GitHub named no tree for the tip of '{branch}' on '{org}/{repo}'"
            )
        return commit_sha, tree_sha

    def set_ci_vars(
        self,
        org: str,
        repo: str,
        variables: "dict[str, str]",
        *,
        scope: Optional[str],
        token: str,
        base_url: Optional[str] = None,
    ) -> None:
        """``RepoProvider.set_ci_vars`` — set the CI variables a build reads.

        ``scope`` is the PROVIDER's own grouping name, not a stage: ``None`` writes the
        repository-wide Actions ``vars``, a string writes that GitHub *Environment*'s vars.
        AGP names no stage here, so a tenant with three stages needs no change to this
        method. A named scope must ALREADY EXIST on GitHub (environment-scoped vars 404
        otherwise) — creating one would be a second write, and E28B's materialize path
        deliberately uses ``scope=None`` only.

        Idempotent: POST creates, a 409 (already there) PATCHes. An empty value is SKIPPED
        rather than written, so an unset optional variable stays absent instead of arriving
        as an empty string a build cannot tell from a deliberate blank.
        """
        base = (base_url or GITHUB_DEFAULT_BASE).rstrip("/")
        headers = self._headers(token)
        if scope:
            vars_base = f"{base}/repos/{org}/{repo}/environments/{scope}/variables"
            where = f"'{org}/{repo}' (scope '{scope}')"
        else:
            vars_base = f"{base}/repos/{org}/{repo}/actions/variables"
            where = f"'{org}/{repo}'"
        client = self._client or httpx.Client()
        try:
            for name, value in variables.items():
                if value is None or value == "":
                    continue
                resp = client.post(vars_base, headers=headers, json={"name": name, "value": value})
                if resp.status_code == 409:
                    resp = client.patch(
                        f"{vars_base}/{name}", headers=headers, json={"name": name, "value": value}
                    )
                if resp.status_code not in (200, 201, 204):
                    raise GitHubRepoError(
                        f"failed to set CI variable on {where} (HTTP {resp.status_code})"
                    )
        except httpx.HTTPError as exc:
            logger.exception("[repo-provider] GitHub set-ci-vars failed for %s/%s", org, repo)
            raise GitHubRepoError(
                f"GitHub request failed while setting CI variables on {where}: "
                f"{type(exc).__name__}"
            ) from None
        finally:
            if self._owns_client and self._client is None:
                client.close()

    def ensure_pipeline(
        self,
        org: str,
        repo: str,
        *,
        yaml_path: str,
        token: str,
        base_url: Optional[str] = None,
    ) -> Optional[str]:
        """``RepoProvider.ensure_pipeline`` — a NO-OP on GitHub. Always returns ``None``,
        and **``None`` IS SUCCESS**.

        GitHub Actions registers a workflow from the committed YAML itself, so there is
        nothing for AGP to create and nothing to remember. The method exists because Azure
        DevOps does NOT: it needs a pipeline object created and its id stored so a repo
        delete can remove it. Discovering that asymmetry after the interface had four
        methods would have made the second provider a refactor instead of an adapter.

        A CALLER MUST NOT READ THE FALSY RETURN AS A FAILURE. Three of four providers answer
        ``None`` here; a caller that raised on it would break all three while looking
        correct against the one. Failure is an exception, never a ``None``.

        Issues NO request — a no-op that spends a round trip is not a no-op — so ``token``
        goes unused. It stays in the signature because it is part of the portable contract
        the Azure DevOps adapter needs, and a per-provider signature is not a seam.
        """
        logger.debug(
            "[repo-provider] ensure_pipeline is a no-op on GitHub for %s/%s (%s) — Actions "
            "registers the committed workflow itself",
            org,
            repo,
            yaml_path,
        )
        return None

    def read_pr(
        self,
        org: str,
        repo: str,
        number: int,
        *,
        token: str,
        base_url: Optional[str] = None,
    ) -> PullRequestView:
        """``RepoProvider.read_pr`` — read pull request ``number`` as a
        :class:`~models.repository.PullRequestView`.

        The projection is ``github_pr_service``'s, reused rather than re-written: it derives
        ``state`` so a merged PR reads "merged" instead of GitHub's "closed", folds
        ``mergeable`` so absent never becomes ``False``, and falls back to an EMPTY author
        rather than to a login. A second copy of those rules would drift within one epic.

        ``can_approve`` IS ALWAYS ``False`` HERE, with the "no viewer" reason. The portable
        contract carries no ``viewer_login``, so this method cannot establish WHOSE account
        a review would be taken as — and approval standing that nobody established must
        fail closed, or the seam becomes a way to offer a self-approval GitHub then refuses.
        A surface that renders an approve button must keep reading through
        :class:`~services.github_pr_service.GitHubPrService`, under the human's own token
        (D15). This is the READ, not the approve path.
        """
        base = (base_url or GITHUB_DEFAULT_BASE).rstrip("/")
        headers = self._headers(token)
        client = self._client or httpx.Client()
        try:
            resp = client.get(f"{base}/repos/{org}/{repo}/pulls/{number}", headers=headers)
            self._require_ok(resp, f"read pull request #{number} on '{org}/{repo}'")
            try:
                body = resp.json()
            except ValueError:
                body = None
            # viewer_login="" — see the docstring: no viewer is establishable through this
            # seam, so approval standing fails closed rather than being guessed.
            view = _project_pull_request(body, "")
            if view is None:
                raise GitHubRepoError(
                    f"GitHub returned an unusable body for pull request #{number} on "
                    f"'{org}/{repo}'"
                )
            return view
        except httpx.HTTPError as exc:
            logger.exception("[repo-provider] GitHub read-pr failed for %s/%s", org, repo)
            raise GitHubRepoError(
                f"GitHub request failed while reading pull request #{number} on "
                f"'{org}/{repo}': {type(exc).__name__}"
            ) from None
        finally:
            if self._owns_client and self._client is None:
                client.close()

    # ------------------------------------------------------------------ #
    # E28C/T1 — the three portable NOUN-READS (D-C1)
    #
    # E28B's seam could CREATE a repository but never look at one, which is why materialize
    # still shipped bytes from the control plane's own disk and why AGP's registry could not be
    # compared against the org's reality. These three close that: the template REPOSITORY
    # becomes the source of truth.
    #
    # ``read_tree`` WALKS THE GIT-DATA API rather than pulling a tarball, and that is a
    # deliberate trade of ~N+1 requests for the two guarantees the seam promises. A tarball is
    # 1–2 requests, but a git archive OMITS submodules silently and a tar cut short simply ENDS —
    # so "raise on a partial" and "raise on an entry that is not a plain file" would have been
    # claims with nothing to check them against. The trees response STATES both: ``truncated``
    # is a flag GitHub sends, and every entry carries its ``mode``/``type``. Against an App
    # installation's 5,000 requests/hour, 26 calls for a 25-file template is not the constraint;
    # a template committed half-complete with a green step beside it is.
    # ------------------------------------------------------------------ #

    def read_tree(
        self,
        org: str,
        repo: str,
        *,
        ref: str,
        token: str,
        base_url: Optional[str] = None,
    ) -> "dict[str, bytes]":
        """``RepoProvider.read_tree`` — the whole tree at ``ref`` as ``{path: bytes}``.

        The INVERSE of :meth:`commit_files`, in the same shape, so what this returns is a
        drop-in for what ``collect_scaffold_files`` produces off disk — that shared shape is
        what lets materialize stop shipping seed bytes.

        ``GET git/trees/{ref}?recursive=1`` once, then one ``GET git/blobs/{sha}`` per file.
        ``recursive=1`` is not optional: a flat listing reports only the top level, so a
        template's ``src/`` would arrive as a directory entry and its files would be missing.

        FOUR REFUSALS, each closing a way a partial tree could look complete:

        * a BLANK ``ref`` — refused before any request. Substituting HEAD would answer "whatever
          the branch is now", which is the mixed-tree defect this parameter exists to prevent;
        * ``truncated: true`` — GitHub caps a recursive listing and says so. The entries that
          arrived are individually valid, which is exactly the danger, and ``commit_files``
          builds with no ``base_tree`` so the missing files would land as DELETIONS on a
          re-push;
        * a SYMLINK (mode 120000) or SUBMODULE (mode 160000 / type ``commit``). A symlink's blob
          content is its target PATH, so carrying it through ``commit_files`` (always mode
          100644) replaces a link with the text of its target; a submodule's content is not in
          this repository at all. Skipping either would materialize a template quietly missing
          part of itself, so both name the offending path and raise;
        * a path that is not root-relative (leading ``/``, or a ``..`` segment). NOT normalised:
          normalising silently rewrites a template author's layout, and passing it on hands a
          traversal to whichever consumer is least careful.

        A ``tree`` entry is the one kind legitimately skipped — with ``recursive=1`` the files'
        paths are already fully qualified, so a directory carries nothing and has no blob to
        fetch. Mode 100755 is carried: an ``entrypoint.sh`` is content, and refusing every
        template that ships a shell script to preserve a bit ``commit_files`` does not write
        anyway would be the wrong trade.

        BYTES, NEVER DECODED (tenet 5). ``encoding`` is CHECKED rather than assumed: GitHub
        answers ``encoding: "none"`` with an empty ``content`` for a blob over its inline size
        limit, and base64-decoding that yields ``b""`` — a real file silently becoming an empty
        one. A 2xx with no ``content`` raises for the same reason :meth:`_sha_of` exists: a
        ``KeyError`` escapes this module's error contract, and a ``None`` would commit an empty
        file over a real one.
        """
        if not ref:
            raise GitHubRepoError(
                f"refusing to read the tree of '{org}/{repo}' without a ref — a caller must "
                f"resolve a head sha and read AT it, or a push landing mid-read yields a tree "
                f"that was never a commit"
            )
        # The ref is interpolated into the URL, so it must not be able to STEER the request.
        # THREE ways it could, all verified against httpx rather than assumed:
        #
        #   ``..``      httpx NORMALISES a traversal before sending (browser behaviour), so
        #               ``…/git/trees/../../../orgs/other/repos`` leaves as
        #               ``/repos/{org}/orgs/other/repos`` — a DIFFERENT GitHub endpoint, asked
        #               under AGP's own App installation token, whose answer this method would
        #               hand back as if it were a template's file tree. A leading slash collapses
        #               the path the same way.
        #   ``?``       ``ref="main?recursive=0"`` MERGES a parameter into the query beside AGP's
        #               own ``recursive=1``. The completeness of this read depends on that
        #               parameter, and a request carrying one AGP did not author is not AGP's
        #               request.
        #   ``#``       ``ref="main#frag"`` is sent as ``main`` with the fragment DROPPED — the
        #               read silently succeeds against a ref that is not the one asked for, which
        #               is the mixed-tree failure this parameter exists to prevent, arriving with
        #               nothing to notice.
        #   ``%``       the same traversal with the ``..`` HIDDEN behind an escape (added by E28C's
        #               final review, which executed it). httpx does NOT decode ``%2e%2e`` or
        #               ``..%2f`` — it sends the escape through literally, so the clause above
        #               matches nothing and the request REACHES THE WIRE, leaving refusal to
        #               GitHub's server-side decoding. Depending on a server's decoding is exactly
        #               what the ``..`` clause refuses to do, so ``%`` is refused here instead.
        #
        # ``ref`` is not operator-typed — it arrives from a DynamoDB template record or a provider
        # response — so this is the URL counterpart of the disk traversal E28B's final review
        # closed. REFUSED, not escaped or sanitised: a caller holding a malformed ref has a bug
        # that should be stated. None of these characters is legal in a git ref name (``git
        # check-ref-format`` forbids ``..``, ``?``, ``%`` and ``~^:``), so nothing legitimate is
        # turned away — ``refs/heads/main``, ``feature/x`` and ``v1.2.3`` all still pass.
        if (
            ref.startswith(("/", "\\"))
            or ".." in ref.replace("\\", "/").split("/")
            or "?" in ref
            or "#" in ref
            or "%" in ref
        ):
            raise GitHubRepoError(
                f"refusing to read the tree of '{org}/{repo}' at a ref that is not a plain "
                f"git ref — a ref may not start at the root, contain a path traversal or a "
                f"percent-escape, or carry a query or fragment"
            )
        base = (base_url or GITHUB_DEFAULT_BASE).rstrip("/")
        headers = self._headers(token)
        repo_base = f"{base}/repos/{org}/{repo}"
        client = self._client or httpx.Client()
        try:
            listing = client.get(
                f"{repo_base}/git/trees/{ref}", headers=headers, params={"recursive": "1"}
            )
            self._require_ok(listing, f"read the tree at '{ref}' on '{org}/{repo}'")
            try:
                body = listing.json()
            except ValueError:
                body = None
            if not isinstance(body, dict) or not isinstance(body.get("tree"), list):
                raise GitHubRepoError(
                    f"GitHub returned an unusable tree listing for '{ref}' on '{org}/{repo}'"
                )
            if body.get("truncated"):
                raise GitHubRepoError(
                    f"GitHub truncated the tree listing for '{ref}' on '{org}/{repo}' — "
                    f"refusing a partial tree, because committing one would silently ship an "
                    f"incomplete template"
                )

            files: dict[str, bytes] = {}
            for entry in body["tree"]:
                if not isinstance(entry, dict):
                    raise GitHubRepoError(
                        f"GitHub returned an unusable tree entry for '{ref}' on '{org}/{repo}'"
                    )
                path = entry.get("path")
                if not isinstance(path, str) or not path:
                    raise GitHubRepoError(
                        f"GitHub named no path for a tree entry of '{ref}' on '{org}/{repo}'"
                    )
                mode, kind = entry.get("mode"), entry.get("type")
                if kind == "tree":
                    continue  # a DIRECTORY: with recursive=1 its files are listed separately
                if mode == "120000":
                    raise GitHubRepoError(
                        f"'{path}' on '{org}/{repo}' is a symlink, which AGP cannot carry as "
                        f"content — its blob holds a target path, not a file"
                    )
                if mode == "160000" or kind == "commit":
                    raise GitHubRepoError(
                        f"'{path}' on '{org}/{repo}' is a submodule, whose content is not in "
                        f"this repository — AGP cannot materialize it"
                    )
                if mode not in ("100644", "100755"):
                    raise GitHubRepoError(
                        f"'{path}' on '{org}/{repo}' is not a plain file (mode {mode!r}) — "
                        f"AGP carries file bytes only"
                    )
                _require_root_relative(path, org, repo)
                sha = entry.get("sha")
                if not isinstance(sha, str) or not sha:
                    raise GitHubRepoError(
                        f"GitHub named no sha for '{path}' on '{org}/{repo}'"
                    )
                files[path] = self._read_blob(client, repo_base, headers, org, repo, path, sha)
            return files
        except httpx.HTTPError as exc:
            logger.exception("[repo-provider] GitHub read-tree failed for %s/%s", org, repo)
            raise GitHubRepoError(
                f"GitHub request failed while reading the tree at '{ref}' on '{org}/{repo}': "
                f"{type(exc).__name__}"
            ) from None
        finally:
            if self._owns_client and self._client is None:
                client.close()

    def _read_blob(self, client, repo_base, headers, org, repo, path: str, sha: str) -> bytes:
        """One blob's BYTES, or ``GitHubRepoError``. Never a partial and never a decoded ``str``.

        The read-path counterpart of :meth:`_sha_of`: a 2xx whose body lacks ``content``, names
        an ``encoding`` other than base64, or does not decode is a provider surprise, and saying
        so beats the two alternatives — a ``KeyError`` that escapes this module's error contract,
        or an EMPTY file committed over a real one."""
        resp = client.get(f"{repo_base}/git/blobs/{sha}", headers=headers)
        self._require_ok(resp, f"read the blob for '{path}' on '{org}/{repo}'")
        try:
            body = resp.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise GitHubRepoError(
                f"GitHub returned an unusable blob body for '{path}' on '{org}/{repo}'"
            )
        encoding = body.get("encoding")
        if encoding != "base64":
            # ``none`` is what GitHub answers for a blob over its inline size limit, with an
            # EMPTY content — decoding it would turn a real file into a zero-byte one.
            raise GitHubRepoError(
                f"GitHub served '{path}' on '{org}/{repo}' with encoding {encoding!r} rather "
                f"than base64 — refusing to guess at its bytes"
            )
        content = body.get("content")
        if not isinstance(content, str):
            raise GitHubRepoError(
                f"GitHub named no content for '{path}' on '{org}/{repo}'"
            )
        try:
            return base64.b64decode(content)
        except (ValueError, binascii.Error):
            raise GitHubRepoError(
                f"GitHub served undecodable base64 for '{path}' on '{org}/{repo}'"
            ) from None

    def read_repo(
        self,
        org: str,
        repo: str,
        *,
        token: str,
        base_url: Optional[str] = None,
    ) -> "Optional[RepoView]":
        """``RepoProvider.read_repo`` — existence, default branch, head sha. The reconcile probe.

        TWO GETs, because GitHub's repository object names ``default_branch`` but carries no tip
        sha and no single endpoint carries both. That is adapter-internal plumbing behind a
        one-call seam; the second GET reads ``git/ref/heads/{default_branch}``.

        The default branch is READ, never assumed to be ``main``: a customer's template repo may
        use any trunk, and guessing would read the wrong tree or none at all. The ``head_sha``
        this returns is what a caller hands back as :meth:`read_tree`'s ``ref`` — resolve once,
        read at that sha.

        **``None`` MEANS NOT-FOUND AND NOTHING ELSE.** A 401, 403, 500 or a transport failure
        RAISES. This is the most consequential rule in the method: ``None`` is what makes a
        registry row read ``registered_missing`` and offers an operator "re-create from seed", so
        folding an outage into it would propose overwriting a customer's iterated template repos
        with starter bytes because a token expired. "AGP could not look" is not evidence of
        absence.

        A repository that exists with NO COMMIT yet answers a view with an EMPTY ``head_sha`` —
        present with nothing to read. Not ``None`` (reconcile would offer to create what is
        already there, and the create would 422) and not a raise (nothing failed).
        :meth:`create_repo` produces exactly this state, so the seam has to be able to say it.

        Shares :meth:`_read_repo_body` with :meth:`repo_exists` — ONE authority for "what does
        GET /repos/{org}/{repo} mean", so the existence probe and this projection cannot drift
        into disagreeing about which statuses are benign.
        """
        base = (base_url or GITHUB_DEFAULT_BASE).rstrip("/")
        headers = self._headers(token)
        client = self._client or httpx.Client()
        try:
            body = self._read_repo_body(client, base, headers, org, repo)
            if body is None:
                return None
            branch = body.get("default_branch")
            if not isinstance(branch, str) or not branch:
                raise GitHubRepoError(
                    f"GitHub named no default branch for '{org}/{repo}'"
                )
            ref_get = client.get(
                f"{base}/repos/{org}/{repo}/git/ref/heads/{branch}", headers=headers
            )
            if ref_get.status_code == 404:
                # PRESENT, with nothing on it yet — see the docstring.
                return RepoView(default_branch=branch, head_sha="")
            self._require_ok(ref_get, f"read branch '{branch}' on '{org}/{repo}'")
            try:
                ref_body = ref_get.json()
            except ValueError:
                ref_body = None
            head = (
                (ref_body.get("object") or {}).get("sha")
                if isinstance(ref_body, dict)
                else None
            )
            if not isinstance(head, str) or not head:
                raise GitHubRepoError(
                    f"GitHub named no commit for branch '{branch}' on '{org}/{repo}'"
                )
            return RepoView(default_branch=branch, head_sha=head)
        except httpx.HTTPError as exc:
            logger.exception("[repo-provider] GitHub read-repo failed for %s/%s", org, repo)
            raise GitHubRepoError(
                f"GitHub request failed while reading '{org}/{repo}': {type(exc).__name__}"
            ) from None
        finally:
            if self._owns_client and self._client is None:
                client.close()

    def _read_repo_body(self, client, base, headers, org, repo) -> "Optional[dict]":
        """``GET /repos/{org}/{repo}``: the body on 200, ``None`` on 404, a raise otherwise.

        The ONE place that decides what a status on this endpoint means. :meth:`repo_exists` and
        :meth:`read_repo` both read it — a second copy of "404 is absence, 403 is not" is how the
        existence probe and the reconcile projection would come to disagree about whether an
        expired token means a repository is gone."""
        resp = client.get(f"{base}/repos/{org}/{repo}", headers=headers)
        if resp.status_code == 404:
            return None
        self._require_ok(resp, f"read repo '{org}/{repo}'")
        try:
            body = resp.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise GitHubRepoError(f"GitHub returned an unusable body for '{org}/{repo}'")
        return body

    def list_repos(
        self,
        org: str,
        *,
        token: str,
        base_url: Optional[str] = None,
    ) -> "list[str]":
        """``RepoProvider.list_repos`` — the NAMES of ``org``'s repositories.

        ``GET /orgs/{org}/repos``, paginated internally until a short page ends the walk. Names
        only: the caller subtracts what AGP already accounts for (registered templates,
        materialized agent repos, the infra repo) and offers the remainder for adoption, and a
        name is all that needs.

        **NO FILTER, not even a query parameter.** This is not the ``list_template_repos`` E28B
        deleted returning under a portable name — the unportable part of that method was its
        ``is_template`` flag, a GitHub-only concept, and admitting it as an argument OR as a
        hidden query parameter would put a provider concept back in the seam. What is a template
        is a human's statement, made by adopting a repo (D-C4).

        A short page ends the walk rather than an empty one: reading until empty spends an extra
        request on every call, and this surface is costed against Bitbucket's 1,000 req/h (D-C3).

        THE PAGE CAP RAISES. Returning what it had would be the same silent partial
        :meth:`read_tree` refuses, one layer up — an operator would see a list that looks like
        their whole org and adopt against it. Reaching the cap is either an org far larger than
        this surface was designed for or a pagination bug, and both are worth stating.
        """
        base = (base_url or GITHUB_DEFAULT_BASE).rstrip("/")
        headers = self._headers(token)
        client = self._client or httpx.Client()
        names: list[str] = []
        try:
            for page in range(1, _LIST_REPOS_MAX_PAGES + 1):
                resp = client.get(
                    f"{base}/orgs/{org}/repos",
                    headers=headers,
                    params={"per_page": _LIST_REPOS_PER_PAGE, "page": page},
                )
                self._require_ok(resp, f"list the repositories of '{org}'")
                try:
                    body = resp.json()
                except ValueError:
                    body = None
                if not isinstance(body, list):
                    raise GitHubRepoError(
                        f"GitHub returned an unusable repository list for '{org}'"
                    )
                for item in body:
                    name = item.get("name") if isinstance(item, dict) else None
                    if not isinstance(name, str) or not name:
                        raise GitHubRepoError(
                            f"GitHub named no name for a repository of '{org}'"
                        )
                    names.append(name)
                if len(body) < _LIST_REPOS_PER_PAGE:
                    return names
            raise GitHubRepoError(
                f"'{org}' has more repositories than this adapter will page through "
                f"({_LIST_REPOS_MAX_PAGES} pages of {_LIST_REPOS_PER_PAGE}) — refusing to "
                f"return a silently capped list"
            )
        except httpx.HTTPError as exc:
            logger.exception("[repo-provider] GitHub list-repos failed for %s", org)
            raise GitHubRepoError(
                f"GitHub request failed while listing the repositories of '{org}': "
                f"{type(exc).__name__}"
            ) from None
        finally:
            if self._owns_client and self._client is None:
                client.close()

    @staticmethod
    def _headers(token: str) -> "dict[str, str]":
        """The per-request auth + Accept headers.

        A HELPER, not a stored attribute: the token must never live on the instance, or one
        client could carry one caller's credential onto another's call. Same shape every
        method above builds inline."""
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        }

    # ------------------------------------------------------------------ #
    # Steps
    # ------------------------------------------------------------------ #

    def _create_repo(self, client, base, headers, org, repo_name):
        # auto_init=true: GitHub seeds an initial commit so the repo's git database exists.
        # Without it the git-data API (blobs/trees/commits) 409s "Git Repository is empty".
        resp = client.post(
            f"{base}/orgs/{org}/repos",
            headers=headers,
            json={"name": repo_name, "private": True, "auto_init": True},
        )
        if resp.status_code not in (200, 201):
            hint = ""
            if resp.status_code == 422:
                # Almost always "name already exists on this account" — the common re-run case.
                hint = " — the repo may already exist in the org (delete it and retry)"
            raise GitHubRepoError(
                f"could not create repo '{org}/{repo_name}' (HTTP {resp.status_code}: "
                f"{_github_message(resp)}){hint}"
            )
        body = resp.json()
        owner = (body.get("owner") or {}).get("login") or org
        return body.get("html_url") or f"{base}/{org}/{repo_name}", owner

    def _push_initial_commit(self, client, base, headers, owner, repo_name, files):
        """Push ``files`` as this repo's initial commit (blobs → tree → commit → ref).

        E28B/T7 — EVERY sha read here goes through :meth:`_sha_of`. A bare
        ``resp.json()["sha"]`` raises ``KeyError`` on a 2xx that carries no ``sha``, and a
        ``KeyError`` ESCAPES this module's error contract: ``create_repo_from_zip`` catches
        ``httpx.HTTPError`` and every CALLER catches ``GitHubRepoError``, so a shape-surprising
        200 propagated as an unhandled type through both. :meth:`_require_ok` cannot cover it —
        the status IS 2xx and it is the BODY that is unusable. Same defect T1 closed in
        :meth:`commit_files`; the four reads below were the last of it.
        """
        repo_base = f"{base}/repos/{owner}/{repo_name}"

        # The repo was auto-init'd, so a branch + parent commit already exist. Get that
        # commit's sha to parent the scaffold commit onto (and to fast-forward afterwards).
        ref_get = client.get(f"{repo_base}/git/ref/heads/{_DEFAULT_BRANCH}", headers=headers)
        self._require_ok(ref_get, "read default branch ref")
        # ``.get()`` chained, mirroring ``_read_branch_tip``: the sha is NESTED under ``object``
        # here, so ``_sha_of`` (which reads the top level) does not apply — but an absent
        # ``object`` must still be a GitHubRepoError rather than a KeyError.
        try:
            ref_body = ref_get.json()
        except ValueError:
            ref_body = None
        parent_sha = (
            (ref_body.get("object") or {}).get("sha") if isinstance(ref_body, dict) else None
        )
        if not parent_sha:
            raise GitHubRepoError(
                f"GitHub named no commit for the default branch of '{owner}/{repo_name}'"
            )

        tree_items = []
        for path, content in files.items():
            blob_resp = client.post(
                f"{repo_base}/git/blobs",
                headers=headers,
                json={"content": base64.b64encode(content).decode("ascii"), "encoding": "base64"},
            )
            self._require_ok(blob_resp, "create blob")
            tree_items.append(
                {
                    "path": path,
                    "mode": "100644",
                    "type": "blob",
                    "sha": self._sha_of(
                        blob_resp, f"the blob for '{path}' on '{owner}/{repo_name}'"
                    ),
                }
            )

        # Build the scaffold tree WITHOUT a base_tree — the resulting commit contains exactly
        # the scaffold files (the auto-init README is not carried forward).
        tree_resp = client.post(f"{repo_base}/git/trees", headers=headers, json={"tree": tree_items})
        self._require_ok(tree_resp, "create tree")

        commit_resp = client.post(
            f"{repo_base}/git/commits",
            headers=headers,
            json={
                "message": _INITIAL_COMMIT_MESSAGE,
                "tree": self._sha_of(tree_resp, f"the tree on '{owner}/{repo_name}'"),
                "parents": [parent_sha],
            },
        )
        self._require_ok(commit_resp, "create commit")

        # Fast-forward the existing branch to the scaffold commit (force past the auto-init commit).
        ref_resp = client.patch(
            f"{repo_base}/git/refs/heads/{_DEFAULT_BRANCH}",
            headers=headers,
            json={
                "sha": self._sha_of(commit_resp, f"the commit on '{owner}/{repo_name}'"),
                "force": True,
            },
        )
        self._require_ok(ref_resp, "update ref")

    @staticmethod
    def _require_ok(resp, what: str) -> None:
        if resp.status_code not in (200, 201):
            raise GitHubRepoError(
                f"failed to {what} (HTTP {resp.status_code}: {_github_message(resp)})"
            )


def _github_message(resp) -> str:
    """Extract GitHub's error ``message`` (+ any ``errors[].message``) for a readable, SAFE
    reason. GitHub's error bodies are validation strings (e.g. "name already exists on this
    account"), never credential material. Falls back to a fixed string if the body is not the
    expected JSON shape — we never surface a raw/HTML body."""
    try:
        body = resp.json()
    except Exception:
        return "no error detail"
    if not isinstance(body, dict):
        return "no error detail"
    msg = body.get("message") or ""
    errors = body.get("errors")
    if isinstance(errors, list):
        details = [
            e.get("message") for e in errors if isinstance(e, dict) and e.get("message")
        ]
        if details:
            msg = f"{msg}: {'; '.join(details)}" if msg else "; ".join(details)
    return msg or "no error detail"


def _require_root_relative(path: str, org: str, repo: str) -> None:
    """A tree path must be ROOT-RELATIVE: no leading slash, no ``..`` segment, no drive letter.

    NOT a normalisation. Rewriting the path would silently change a template author's layout, and
    passing it through hands a traversal to whichever consumer is least careful — the seed path's
    sibling code writes to disk, and E28B's final review closed exactly that hole there. GitHub
    does not produce such a path; a provider that did is a surprise worth stating."""
    if path.startswith(("/", "\\")) or ":" in path.split("/")[0]:
        raise GitHubRepoError(
            f"'{path}' on '{org}/{repo}' is not root-relative — refusing an absolute tree path"
        )
    if ".." in path.split("/"):
        raise GitHubRepoError(
            f"'{path}' on '{org}/{repo}' escapes the tree root — refusing a traversal segment"
        )


def _read_zip(zip_bytes: bytes) -> "dict[str, bytes]":
    """Unpack a zip into ``{path: content}``, skipping directory entries."""
    files: dict[str, bytes] = {}
    with zipfile.ZipFile(io.BytesIO(zip_bytes), mode="r") as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            files[info.filename] = zf.read(info.filename)
    return files
