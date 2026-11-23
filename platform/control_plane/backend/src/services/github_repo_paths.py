"""The GitHub adapter's module functions: the response checks, the ref and tree-entry guards, and the
zip-scaffold push behind ``GitHubRepoService.create_repo_from_zip``."""

from __future__ import annotations

import base64
import io
import zipfile
from typing import Optional

import httpx

from services.github_repo_errors import GitHubRepoError

_DEFAULT_BRANCH = "main"
_INITIAL_COMMIT_MESSAGE = "Initial commit from AGP agent template"
_FILE_MODES = ("100644", "100755")


def _require_ok(resp, what: str) -> None:
    if resp.status_code not in (200, 201):
        raise GitHubRepoError(f"failed to {what} (HTTP {resp.status_code}: {_github_message(resp)})")


def _sha_of(resp, what: str) -> str:
    """The ``sha`` from a git-data 2xx body, or ``GitHubRepoError``: a 2xx whose body is not
    the shape GitHub documents is a provider surprise, and a ``KeyError`` would escape the
    error contract every caller relies on."""
    try:
        body = resp.json()
    except ValueError:
        body = None
    sha = body.get("sha") if isinstance(body, dict) else None
    if not isinstance(sha, str) or not sha:
        raise GitHubRepoError(f"GitHub named no sha for {what}")
    return sha


def _github_message(resp) -> str:
    """GitHub's error ``message`` plus any ``errors[].message``: validation strings, never
    credential material. A body that is not the documented JSON shape yields a fixed string, so a
    raw or HTML body never surfaces."""
    try:
        body = resp.json()
    except ValueError:
        return "no error detail"
    if not isinstance(body, dict):
        return "no error detail"
    msg = body.get("message") or ""
    errors = body.get("errors")
    if isinstance(errors, list):
        details = [e["message"] for e in errors if isinstance(e, dict) and e.get("message")]
        if details:
            msg = f"{msg}: {'; '.join(details)}" if msg else "; ".join(details)
    return msg or "no error detail"


def _require_plain_ref(ref: str, org: str, repo: str) -> None:
    """Refuse a blank ``ref`` and any ``ref`` that could steer the request. ``httpx`` normalises a
    ``..`` traversal before sending (the request would reach a different endpoint under AGP's own
    token), merges a ``?`` parameter beside AGP's ``recursive=1``, drops a ``#`` fragment (the read
    would succeed against a different ref) and sends a percent-escape through literally (leaving
    refusal to the server). None of these is legal in a git ref name."""
    if not ref:
        raise GitHubRepoError(
            f"refusing to read the tree of '{org}/{repo}' without a ref — a caller must "
            f"resolve a head sha and read AT it, or a push landing mid-read yields a tree "
            f"that was never a commit"
        )
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


def _tree_entries(listing: httpx.Response, ref: str, org: str, repo: str) -> list:
    """The ``tree`` list of a 2xx listing, or ``GitHubRepoError`` for a body of the wrong shape or
    a listing GitHub truncated: the entries that arrived are individually valid, which is the
    danger, and ``commit_files`` would land the missing files as deletions on a re-push."""
    try:
        body = listing.json()
    except ValueError:
        body = None
    if not isinstance(body, dict) or not isinstance(body.get("tree"), list):
        raise GitHubRepoError(f"GitHub returned an unusable tree listing for '{ref}' on '{org}/{repo}'")
    if body.get("truncated"):
        raise GitHubRepoError(
            f"GitHub truncated the tree listing for '{ref}' on '{org}/{repo}' — "
            f"refusing a partial tree, because committing one would silently ship an "
            f"incomplete template"
        )
    return body["tree"]


def _file_entry(entry, ref: str, org: str, repo: str) -> Optional[tuple[str, str]]:
    """``(path, blob sha)`` for a plain-file entry, ``None`` for a directory, and
    ``GitHubRepoError`` for anything AGP cannot carry: a symlink (its blob is a target path, which
    ``commit_files`` would write back as a regular file), a submodule (its content is not in this
    repository), another mode, a path that is not root-relative, or an entry with no path or sha.
    Mode 100755 is carried: an executable is content."""
    if not isinstance(entry, dict):
        raise GitHubRepoError(f"GitHub returned an unusable tree entry for '{ref}' on '{org}/{repo}'")
    path = entry.get("path")
    if not isinstance(path, str) or not path:
        raise GitHubRepoError(f"GitHub named no path for a tree entry of '{ref}' on '{org}/{repo}'")
    mode, kind = entry.get("mode"), entry.get("type")
    if kind == "tree":
        return None
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
    if mode not in _FILE_MODES:
        raise GitHubRepoError(
            f"'{path}' on '{org}/{repo}' is not a plain file (mode {mode!r}) — AGP carries file bytes only"
        )
    _require_root_relative(path, org, repo)
    sha = entry.get("sha")
    if not isinstance(sha, str) or not sha:
        raise GitHubRepoError(f"GitHub named no sha for '{path}' on '{org}/{repo}'")
    return path, sha


def _require_root_relative(path: str, org: str, repo: str) -> None:
    """A tree path must be root-relative: no leading slash, no ``..`` segment, no drive letter.
    Refused rather than normalised, since rewriting would silently change a template author's
    layout and passing it on would hand a traversal to the least careful consumer."""
    if path.startswith(("/", "\\")) or ":" in path.split("/")[0]:
        raise GitHubRepoError(
            f"'{path}' on '{org}/{repo}' is not root-relative — refusing an absolute tree path"
        )
    if ".." in path.split("/"):
        raise GitHubRepoError(
            f"'{path}' on '{org}/{repo}' escapes the tree root — refusing a traversal segment"
        )


def _create_repo(client, base, headers, org, repo_name):
    resp = client.post(
        f"{base}/orgs/{org}/repos",
        headers=headers,
        json={"name": repo_name, "private": True, "auto_init": True},
    )
    if resp.status_code not in (200, 201):
        hint = ""
        if resp.status_code == 422:
            hint = " — the repo may already exist in the org (delete it and retry)"
        raise GitHubRepoError(
            f"could not create repo '{org}/{repo_name}' (HTTP {resp.status_code}: "
            f"{_github_message(resp)}){hint}"
        )
    body = resp.json()
    owner = (body.get("owner") or {}).get("login") or org
    return body.get("html_url") or f"{base}/{org}/{repo_name}", owner


def _push_initial_commit(client, base, headers, owner, repo_name, files):
    """Push ``files`` as this repo's initial commit: read the auto-init parent, blobs, a tree
    with no ``base_tree`` (the README is not carried forward), a commit, a forced ref update."""
    repo_base = f"{base}/repos/{owner}/{repo_name}"

    ref_get = client.get(f"{repo_base}/git/ref/heads/{_DEFAULT_BRANCH}", headers=headers)
    _require_ok(ref_get, "read default branch ref")
    try:
        ref_body = ref_get.json()
    except ValueError:
        ref_body = None
    parent_sha = (ref_body.get("object") or {}).get("sha") if isinstance(ref_body, dict) else None
    if not parent_sha:
        raise GitHubRepoError(f"GitHub named no commit for the default branch of '{owner}/{repo_name}'")

    tree_items = []
    for path, content in files.items():
        blob_resp = client.post(
            f"{repo_base}/git/blobs",
            headers=headers,
            json={"content": base64.b64encode(content).decode("ascii"), "encoding": "base64"},
        )
        _require_ok(blob_resp, "create blob")
        tree_items.append(
            {
                "path": path,
                "mode": "100644",
                "type": "blob",
                "sha": _sha_of(blob_resp, f"the blob for '{path}' on '{owner}/{repo_name}'"),
            }
        )

    tree_resp = client.post(f"{repo_base}/git/trees", headers=headers, json={"tree": tree_items})
    _require_ok(tree_resp, "create tree")

    commit_resp = client.post(
        f"{repo_base}/git/commits",
        headers=headers,
        json={
            "message": _INITIAL_COMMIT_MESSAGE,
            "tree": _sha_of(tree_resp, f"the tree on '{owner}/{repo_name}'"),
            "parents": [parent_sha],
        },
    )
    _require_ok(commit_resp, "create commit")

    ref_resp = client.patch(
        f"{repo_base}/git/refs/heads/{_DEFAULT_BRANCH}",
        headers=headers,
        json={
            "sha": _sha_of(commit_resp, f"the commit on '{owner}/{repo_name}'"),
            "force": True,
        },
    )
    _require_ok(ref_resp, "update ref")


def _read_zip(zip_bytes: bytes) -> dict[str, bytes]:
    """Unpack a zip into ``{path: content}``, skipping directory entries."""
    files: dict[str, bytes] = {}
    with zipfile.ZipFile(io.BytesIO(zip_bytes), mode="r") as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            files[info.filename] = zf.read(info.filename)
    return files
