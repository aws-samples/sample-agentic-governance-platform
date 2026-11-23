"""GitHub org write client and the GitHub adapter of the portable :class:`RepoProvider` seam.

A thin ``httpx`` client in the shape of ``connection_verify``: a base URL, ``Authorization: Bearer
{token}`` set on each request, an injectable ``httpx.Client``. It owns the zip-scaffold rollout path
(``create_repo_from_zip``, used for template and infra repos: create the repo with ``auto_init``,
then push the zip's contents as one commit through the git-data API), the delete/probe pair, the
trunk adoption pair, and every portable verb of :mod:`services.repo_provider`.

The token is used only as a request header; it is never logged and never folded into an exception
message. A :class:`GitHubRepoError` message carries GitHub's own validation text at most, never a
raw body, and a transport failure surfaces the exception type only, re-raised ``from None``. A 2xx
whose body is not the shape GitHub documents raises ``GitHubRepoError`` rather than a ``KeyError``,
because every caller catches ``GitHubRepoError`` and nothing else.
"""

from __future__ import annotations

import base64
import binascii
import logging
from collections.abc import Mapping
from typing import Optional
from urllib.parse import quote

import httpx

from services.github_repo_errors import GitHubRepoConflictError, GitHubRepoError
from services.github_repo_paths import (
    _create_repo,
    _file_entry,
    _github_message,
    _push_initial_commit,
    _read_zip,
    _require_ok,
    _require_plain_ref,
    _sha_of,
    _tree_entries,
)
from services.repo_provider import RepoView

logger = logging.getLogger(__name__)

GITHUB_DEFAULT_BASE = "https://api.github.com"

_LIST_REPOS_PER_PAGE = 100
_LIST_REPOS_MAX_PAGES = 20


class GitHubRepoService:
    """Thin GitHub write client. ``client`` is injectable for tests and carries no pre-set auth;
    auth is per request."""

    def __init__(self, *, client: Optional[httpx.Client] = None) -> None:
        self._client = client
        self._owns_client = client is None

    def create_repo_from_zip(
        self,
        org: str,
        repo_name: str,
        zip_bytes: bytes,
        token: str,
        base_url: Optional[str] = None,
    ) -> str:
        """Create ``repo_name`` under ``org`` and push the zip's contents as the initial commit;
        return the repo's ``html_url``. Raises ``GitHubRepoError`` on any API failure."""
        base = (base_url or GITHUB_DEFAULT_BASE).rstrip("/")
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        }

        client = self._client or httpx.Client()
        try:
            html_url, owner = _create_repo(client, base, headers, org, repo_name)
            files = _read_zip(zip_bytes)
            if files:
                _push_initial_commit(client, base, headers, owner, repo_name, files)
            return html_url
        except httpx.HTTPError as exc:
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
        """``DELETE /repos/{org}/{repo}``. 204 is success and so is 404 (already gone), so the
        delete is idempotent; any other non-2xx raises ``GitHubRepoError``."""
        base = (base_url or GITHUB_DEFAULT_BASE).rstrip("/")
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        }
        client = self._client or httpx.Client()
        try:
            resp = client.delete(f"{base}/repos/{org}/{repo}", headers=headers)
            if resp.status_code not in (200, 202, 204, 404):
                raise GitHubRepoError(
                    f"failed to delete repo '{org}/{repo}' (HTTP {resp.status_code}: {_github_message(resp)})"
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
        """Does ``org/repo`` still exist? 200 is present and 404 is gone; any other non-2xx raises
        ``GitHubRepoError`` (the caller maps a raise to state ``unknown``). Shares
        :meth:`_read_repo_body` with :meth:`read_repo`, so the two never disagree on what a status
        means."""
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

    def create_repo(
        self,
        org: str,
        name: str,
        *,
        private: bool,
        token: str,
        base_url: Optional[str] = None,
    ) -> str:
        """``RepoProvider.create_repo``: make ``org/name`` exist and return its URL.

        ``auto_init=True`` seeds one commit so the repository's git database exists; against a
        truly empty repo the git-data API answers 409 "Git Repository is empty". The seeded README
        does not survive :meth:`commit_files`, which builds its tree with no ``base_tree``.

        Idempotent by a narrow rule: a 422 whose detail says the name already exists is a benign
        re-run, so the URL is read back (the 422 body carries none) instead of raised over. Every
        other 422 fails closed, since 422 is overloaded on this endpoint."""
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
        files: dict[str, bytes],
        *,
        branch: str,
        message: str,
        token: str,
        base_url: Optional[str] = None,
    ) -> str:
        """``RepoProvider.commit_files``: put ``files`` on ``branch`` in one commit; return the
        commit sha.

        Blobs, then a tree with no ``base_tree`` (the branch ends up carrying exactly ``files``),
        then a commit, then the ref: four calls the caller never sees, and one push event. Git
        objects are content-addressed, so identical files yield the tree sha already at the tip;
        then no commit is written, the ref does not move and the tip sha is returned. A branch
        that does not exist yet is created with a root commit and ``POST /git/refs``. An empty
        ``files`` fails closed: an empty tree would delete the branch's contents."""
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
            parent_sha, parent_tree_sha = self._read_branch_tip(client, repo_base, headers, org, repo, branch)

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
                _require_ok(blob_resp, f"create blob for '{path}' on '{org}/{repo}'")
                tree_items.append(
                    {
                        "path": path,
                        "mode": "100644",
                        "type": "blob",
                        "sha": _sha_of(blob_resp, f"the blob for '{path}' on '{org}/{repo}'"),
                    }
                )

            tree_resp = client.post(f"{repo_base}/git/trees", headers=headers, json={"tree": tree_items})
            _require_ok(tree_resp, f"create tree on '{org}/{repo}'")
            tree_sha = _sha_of(tree_resp, f"the tree on '{org}/{repo}'")

            if parent_sha is not None and parent_tree_sha == tree_sha:
                logger.info(
                    "[repo-provider] %s/%s branch '%s' already carries this tree — no commit",
                    org,
                    repo,
                    branch,
                )
                return parent_sha

            commit_resp = client.post(
                f"{repo_base}/git/commits",
                headers=headers,
                json={
                    "message": message,
                    "tree": tree_sha,
                    "parents": [parent_sha] if parent_sha else [],
                },
            )
            _require_ok(commit_resp, f"create commit on '{org}/{repo}'")
            commit_sha = _sha_of(commit_resp, f"the commit on '{org}/{repo}'")

            if parent_sha:
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
            _require_ok(ref_resp, f"update branch '{branch}' on '{org}/{repo}'")
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

    def commit_files_on_head(
        self,
        org: str,
        repo: str,
        files: dict[str, bytes],
        *,
        branch: str,
        expected_head_sha: str,
        message: str,
        token: str,
        base_url: Optional[str] = None,
    ) -> str:
        """``RepoProvider.commit_files_on_head``: overlay ``files`` on the tip, fast-forward only.

        Blobs, a tree with ``base_tree`` = the tip's tree (every other path and its mode is kept),
        a commit parented on ``expected_head_sha``, a ref PATCH with ``force: false``. A tip that
        is not ``expected_head_sha``, or a ref update GitHub refuses as not a fast-forward,
        raises :class:`GitHubRepoConflictError`. An unchanged tree writes nothing."""
        if not files:
            raise GitHubRepoError(f"refusing to commit an empty file set to '{org}/{repo}'")
        if not expected_head_sha:
            raise GitHubRepoError(f"commit_files_on_head on '{org}/{repo}' needs an expected head sha")
        base = (base_url or GITHUB_DEFAULT_BASE).rstrip("/")
        headers = self._headers(token)
        repo_base = f"{base}/repos/{org}/{repo}"
        client = self._client or httpx.Client()
        try:
            tip_sha, tip_tree = self._read_branch_tip(client, repo_base, headers, org, repo, branch)
            if tip_sha != expected_head_sha:
                raise GitHubRepoConflictError(f"branch '{branch}' on '{org}/{repo}' moved")
            tree_items = []
            for path, content in files.items():
                blob_resp = client.post(
                    f"{repo_base}/git/blobs",
                    headers=headers,
                    json={"content": base64.b64encode(content).decode("ascii"), "encoding": "base64"},
                )
                _require_ok(blob_resp, f"create blob for '{path}' on '{org}/{repo}'")
                tree_items.append(
                    {
                        "path": path,
                        "mode": "100644",
                        "type": "blob",
                        "sha": _sha_of(blob_resp, f"the blob for '{path}' on '{org}/{repo}'"),
                    }
                )
            tree_resp = client.post(
                f"{repo_base}/git/trees", headers=headers, json={"base_tree": tip_tree, "tree": tree_items}
            )
            _require_ok(tree_resp, f"create tree on '{org}/{repo}'")
            tree_sha = _sha_of(tree_resp, f"the tree on '{org}/{repo}'")
            if tree_sha == tip_tree:
                return expected_head_sha
            commit_resp = client.post(
                f"{repo_base}/git/commits",
                headers=headers,
                json={"message": message, "tree": tree_sha, "parents": [expected_head_sha]},
            )
            _require_ok(commit_resp, f"create commit on '{org}/{repo}'")
            commit_sha = _sha_of(commit_resp, f"the commit on '{org}/{repo}'")
            ref_resp = client.patch(
                f"{repo_base}/git/refs/heads/{branch}",
                headers=headers,
                json={"sha": commit_sha, "force": False},
            )
            if ref_resp.status_code in (409, 422):
                raise GitHubRepoConflictError(f"branch '{branch}' on '{org}/{repo}' moved")
            _require_ok(ref_resp, f"update branch '{branch}' on '{org}/{repo}'")
            return commit_sha
        except httpx.HTTPError as exc:
            logger.exception("[repo-provider] GitHub commit-on-head failed for %s/%s", org, repo)
            raise GitHubRepoError(
                f"GitHub request failed while committing to '{org}/{repo}' (branch '{branch}'): "
                f"{type(exc).__name__}"
            ) from None
        finally:
            if self._owns_client and self._client is None:
                client.close()

    def read_file(
        self,
        org: str,
        repo: str,
        path: str,
        *,
        ref: str,
        token: str,
        base_url: Optional[str] = None,
    ) -> Optional[bytes]:
        """``RepoProvider.read_file``: the contents API at ``ref``; 404 is ``None``. A file over
        the contents API's inline limit comes back without base64 content, and its bytes are then
        read through the blob it names."""
        if not ref:
            raise GitHubRepoError(f"read_file on '{org}/{repo}' needs a ref")
        base = (base_url or GITHUB_DEFAULT_BASE).rstrip("/")
        headers = self._headers(token)
        repo_base = f"{base}/repos/{org}/{repo}"
        client = self._client or httpx.Client()
        try:
            resp = client.get(f"{repo_base}/contents/{quote(path)}", headers=headers, params={"ref": ref})
            if resp.status_code == 404:
                return None
            _require_ok(resp, f"read '{path}' on '{org}/{repo}'")
            try:
                body = resp.json()
            except ValueError:
                body = None
            if not isinstance(body, dict) or body.get("type") != "file":
                raise GitHubRepoError(f"'{path}' on '{org}/{repo}' is not a file")
            if body.get("encoding") == "base64" and isinstance(body.get("content"), str):
                try:
                    return base64.b64decode(body["content"])
                except (ValueError, binascii.Error):
                    raise GitHubRepoError(
                        f"GitHub served undecodable base64 for '{path}' on '{org}/{repo}'"
                    ) from None
            sha = body.get("sha")
            if not isinstance(sha, str) or not sha:
                raise GitHubRepoError(f"GitHub named no blob for '{path}' on '{org}/{repo}'")
            return self._read_blob(client, repo_base, headers, org, repo, path, sha)
        except httpx.HTTPError as exc:
            logger.exception("[repo-provider] GitHub read-file failed for %s/%s", org, repo)
            raise GitHubRepoError(
                f"GitHub request failed while reading '{path}' on '{org}/{repo}': {type(exc).__name__}"
            ) from None
        finally:
            if self._owns_client and self._client is None:
                client.close()

    def set_default_branch(
        self,
        org: str,
        repo: str,
        branch: str,
        *,
        token: str,
        base_url: Optional[str] = None,
    ) -> None:
        """Point ``org/repo``'s ``default_branch`` at ``branch``; fails closed.

        ``create_repo``'s ``auto_init`` seeds a branch named by the org's default-branch setting,
        so a project whose trunk is another name would otherwise carry two branches with the
        default (the one pull requests open against and ``build.yml`` watches) holding no code.
        The default moves first because GitHub refuses to delete the default branch."""
        base = (base_url or GITHUB_DEFAULT_BASE).rstrip("/")
        client = self._client or httpx.Client()
        try:
            resp = client.patch(
                f"{base}/repos/{org}/{repo}",
                headers=self._headers(token),
                json={"default_branch": branch},
            )
            _require_ok(resp, f"set default branch of '{org}/{repo}' to '{branch}'")
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
        """Delete ``branch`` on ``org/repo``; an already-absent branch (404 or 422) is success, so
        a retried materialize converges. Every other status fails closed."""
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

    def create_branch(
        self,
        org: str,
        repo: str,
        *,
        branch: str,
        from_branch: Optional[str],
        token: str,
        base_url: Optional[str] = None,
    ) -> str:
        """``RepoProvider.create_branch``: ``POST git/refs`` at ``from_branch``'s tip.

        ``from_branch=None`` reads the repository's default branch. A target that already points
        at the source tip is a benign re-run (a 422 on the POST is re-read the same way, for a
        concurrent create); one at any other sha raises :class:`GitHubRepoConflictError`."""
        base = (base_url or GITHUB_DEFAULT_BASE).rstrip("/")
        headers = self._headers(token)
        repo_base = f"{base}/repos/{org}/{repo}"
        client = self._client or httpx.Client()
        try:
            source = from_branch
            if source is None:
                body = self._read_repo_body(client, base, headers, org, repo)
                source = body.get("default_branch") if isinstance(body, dict) else None
                if not isinstance(source, str) or not source:
                    raise GitHubRepoError(f"GitHub named no default branch for '{org}/{repo}'")
            tip, _ = self._read_branch_tip(client, repo_base, headers, org, repo, source)
            if tip is None:
                raise GitHubRepoError(f"branch '{source}' does not exist on '{org}/{repo}'")

            def _converged() -> bool:
                current, _ = self._read_branch_tip(client, repo_base, headers, org, repo, branch)
                if current is not None and current != tip:
                    raise GitHubRepoConflictError(
                        f"branch '{branch}' on '{org}/{repo}' already exists at another commit"
                    )
                return current == tip

            if _converged():
                return tip
            resp = client.post(
                f"{repo_base}/git/refs", headers=headers, json={"ref": f"refs/heads/{branch}", "sha": tip}
            )
            if resp.status_code == 422 and _converged():
                return tip
            _require_ok(resp, f"create branch '{branch}' on '{org}/{repo}'")
            return tip
        except httpx.HTTPError as exc:
            logger.exception("[repo-provider] GitHub create-branch failed for %s/%s", org, repo)
            raise GitHubRepoError(
                f"GitHub request failed while creating branch '{branch}' on '{org}/{repo}': "
                f"{type(exc).__name__}"
            ) from None
        finally:
            if self._owns_client and self._client is None:
                client.close()

    def _read_branch_tip(
        self, client, repo_base, headers, org, repo, branch
    ) -> tuple[Optional[str], Optional[str]]:
        """``(commit_sha, tree_sha)`` for ``branch``, or ``(None, None)`` when it does not exist.

        Two calls, because the ref names a commit and the idempotence gate needs that commit's
        tree. A 404 on the ref is "the branch is new"; any other non-2xx raises, since mistaking
        an unreadable tip for an absent branch would force-create a root commit over history."""
        ref_get = client.get(f"{repo_base}/git/ref/heads/{branch}", headers=headers)
        if ref_get.status_code == 404:
            return None, None
        _require_ok(ref_get, f"read branch '{branch}' on '{org}/{repo}'")
        commit_sha = (ref_get.json().get("object") or {}).get("sha")
        if not commit_sha:
            raise GitHubRepoError(f"GitHub named no commit for branch '{branch}' on '{org}/{repo}'")
        commit_get = client.get(f"{repo_base}/git/commits/{commit_sha}", headers=headers)
        _require_ok(commit_get, f"read the tip commit of '{branch}' on '{org}/{repo}'")
        tree_sha = (commit_get.json().get("tree") or {}).get("sha")
        if not tree_sha:
            raise GitHubRepoError(f"GitHub named no tree for the tip of '{branch}' on '{org}/{repo}'")
        return commit_sha, tree_sha

    def set_ci_vars(
        self,
        org: str,
        repo: str,
        variables: Mapping[str, str | None],
        *,
        scope: Optional[str],
        token: str,
        base_url: Optional[str] = None,
    ) -> None:
        """``RepoProvider.set_ci_vars``: ``None`` scope writes the repository-wide Actions
        ``vars``, a string writes that GitHub Environment's vars (which must already exist).
        POST creates and a 409 PATCHes, so a re-run converges; an empty value is skipped."""
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
                    raise GitHubRepoError(f"failed to set CI variable on {where} (HTTP {resp.status_code})")
        except httpx.HTTPError as exc:
            logger.exception("[repo-provider] GitHub set-ci-vars failed for %s/%s", org, repo)
            raise GitHubRepoError(
                f"GitHub request failed while setting CI variables on {where}: {type(exc).__name__}"
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
        """``RepoProvider.ensure_pipeline``: a no-op on GitHub, which registers a workflow from
        the committed YAML itself. Always ``None``, which is success; no request is issued."""
        logger.debug(
            "[repo-provider] ensure_pipeline is a no-op on GitHub for %s/%s (%s) — Actions "
            "registers the committed workflow itself",
            org,
            repo,
            yaml_path,
        )
        return None

    def read_tree(
        self,
        org: str,
        repo: str,
        *,
        ref: str,
        token: str,
        base_url: Optional[str] = None,
    ) -> dict[str, bytes]:
        """``RepoProvider.read_tree``: the whole tree at ``ref`` as ``{path: bytes}``.

        ``GET git/trees/{ref}?recursive=1`` once, then one ``GET git/blobs/{sha}`` per file: the
        trees response states its truncation and each entry's mode, which a tarball would not.
        Refuses, before any request, a blank or request-steering ``ref``
        (:func:`services.github_repo_paths._require_plain_ref`); refuses a truncated listing
        (:func:`services.github_repo_paths._tree_entries`) and any entry AGP cannot carry as a plain
        file (:func:`services.github_repo_paths._file_entry`); directory entries are
        skipped, since with ``recursive=1`` their files are listed on their own. Values are bytes,
        never decoded, and a blob GitHub would not base64-encode raises rather than becoming an
        empty file."""
        _require_plain_ref(ref, org, repo)
        base = (base_url or GITHUB_DEFAULT_BASE).rstrip("/")
        headers = self._headers(token)
        repo_base = f"{base}/repos/{org}/{repo}"
        client = self._client or httpx.Client()
        try:
            listing = client.get(f"{repo_base}/git/trees/{ref}", headers=headers, params={"recursive": "1"})
            _require_ok(listing, f"read the tree at '{ref}' on '{org}/{repo}'")
            files: dict[str, bytes] = {}
            for entry in _tree_entries(listing, ref, org, repo):
                file_entry = _file_entry(entry, ref, org, repo)
                if file_entry is None:
                    continue
                path, sha = file_entry
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
        """One blob's bytes, or ``GitHubRepoError``: a 2xx whose body lacks ``content``, names an
        ``encoding`` other than base64 (GitHub answers ``none`` with empty content for a blob over
        its inline size limit) or does not decode is a provider surprise, never an empty file."""
        resp = client.get(f"{repo_base}/git/blobs/{sha}", headers=headers)
        _require_ok(resp, f"read the blob for '{path}' on '{org}/{repo}'")
        try:
            body = resp.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise GitHubRepoError(f"GitHub returned an unusable blob body for '{path}' on '{org}/{repo}'")
        encoding = body.get("encoding")
        if encoding != "base64":
            raise GitHubRepoError(
                f"GitHub served '{path}' on '{org}/{repo}' with encoding {encoding!r} rather "
                f"than base64 — refusing to guess at its bytes"
            )
        content = body.get("content")
        if not isinstance(content, str):
            raise GitHubRepoError(f"GitHub named no content for '{path}' on '{org}/{repo}'")
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
    ) -> Optional[RepoView]:
        """``RepoProvider.read_repo``: existence, default branch and head sha, the reconcile probe.

        Two GETs, because GitHub's repository object names ``default_branch`` but carries no tip
        sha. ``None`` means not-found and nothing else: a 401, 403, 500 or transport failure
        raises, because folding an outage into ``None`` would propose overwriting a customer's
        iterated templates with starter bytes. A repository with no commit yet answers a view with
        an empty ``head_sha``. Shares :meth:`_read_repo_body` with :meth:`repo_exists`."""
        base = (base_url or GITHUB_DEFAULT_BASE).rstrip("/")
        headers = self._headers(token)
        client = self._client or httpx.Client()
        try:
            body = self._read_repo_body(client, base, headers, org, repo)
            if body is None:
                return None
            branch = body.get("default_branch")
            if not isinstance(branch, str) or not branch:
                raise GitHubRepoError(f"GitHub named no default branch for '{org}/{repo}'")
            ref_get = client.get(f"{base}/repos/{org}/{repo}/git/ref/heads/{branch}", headers=headers)
            if ref_get.status_code == 404:
                return RepoView(default_branch=branch, head_sha="")
            _require_ok(ref_get, f"read branch '{branch}' on '{org}/{repo}'")
            try:
                ref_body = ref_get.json()
            except ValueError:
                ref_body = None
            head = (ref_body.get("object") or {}).get("sha") if isinstance(ref_body, dict) else None
            if not isinstance(head, str) or not head:
                raise GitHubRepoError(f"GitHub named no commit for branch '{branch}' on '{org}/{repo}'")
            return RepoView(default_branch=branch, head_sha=head)
        except httpx.HTTPError as exc:
            logger.exception("[repo-provider] GitHub read-repo failed for %s/%s", org, repo)
            raise GitHubRepoError(
                f"GitHub request failed while reading '{org}/{repo}': {type(exc).__name__}"
            ) from None
        finally:
            if self._owns_client and self._client is None:
                client.close()

    def _read_repo_body(self, client, base, headers, org, repo) -> Optional[dict]:
        """``GET /repos/{org}/{repo}``: the body on 200, ``None`` on 404, a raise otherwise. The
        one place that decides what a status on this endpoint means."""
        resp = client.get(f"{base}/repos/{org}/{repo}", headers=headers)
        if resp.status_code == 404:
            return None
        _require_ok(resp, f"read repo '{org}/{repo}'")
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
    ) -> list[str]:
        """``RepoProvider.list_repos``: the names of ``org``'s repositories.

        ``GET /orgs/{org}/repos`` paginated until a short page ends the walk, with no filter
        parameter of any kind. The page cap raises rather than returning a silently capped list,
        and so does an item with no usable name: a dropped entry would make an existing repo look
        adoptable."""
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
                _require_ok(resp, f"list the repositories of '{org}'")
                try:
                    body = resp.json()
                except ValueError:
                    body = None
                if not isinstance(body, list):
                    raise GitHubRepoError(f"GitHub returned an unusable repository list for '{org}'")
                for item in body:
                    name = item.get("name") if isinstance(item, dict) else None
                    if not isinstance(name, str) or not name:
                        raise GitHubRepoError(f"GitHub named no name for a repository of '{org}'")
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
                f"GitHub request failed while listing the repositories of '{org}': {type(exc).__name__}"
            ) from None
        finally:
            if self._owns_client and self._client is None:
                client.close()

    @staticmethod
    def _headers(token: str) -> dict[str, str]:
        """The per-request auth and Accept headers; the token never lives on the instance."""
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        }
