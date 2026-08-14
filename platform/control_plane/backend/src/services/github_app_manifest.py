"""GitHub App creation via the manifest flow (E20/U1).

This module owns the "App-via-Manifest" onboarding path: instead of asking an org
admin to hand-create a GitHub App and paste its App ID + private key, AGP hands GitHub
a pre-filled **manifest** and lets the admin click "Create" in their org. GitHub then
redirects back with a one-time ``code`` that AGP exchanges for the App's freshly minted
credentials (App ID, private key PEM, webhook secret). This module builds the manifest,
builds the org-scoped registration URL, converts the ``code``, and — once the admin has
installed the App — resolves the installation id for the target org.

Four deterministic seams, all pure functions over an injected ``httpx.Client``:

  1. **build_manifest / register_url** — no network; produce the manifest JSON and the
     GitHub URL the admin is sent to (``state`` carries CSRF protection, percent-encoded).
     ``build_manifest``'s optional ``callback_urls`` registers where an end *user* lands after
     authorizing the App (E27B) — a different slot from ``redirect_url``, which is where the
     *admin* lands after creating it.
  2. **convert_manifest_code** — POST ``/app-manifests/{code}/conversions``; on 201 GitHub
     returns the new App's ``id`` + ``pem`` + ``webhook_secret`` + ``slug`` and, per its 201
     schema, the OAuth client pair ``client_id`` + ``client_secret`` (E27B — this pair is what
     makes per-user GitHub auth possible; AGP discarded it until now, and GitHub offers NO API
     to recover a ``client_secret`` afterwards — org-admin UI only). All of it is captured and
     handed straight to the credential store — never logged. The pair is read defensively
     (GitHub's prose contradicts its own schema), so its absence must not break onboarding.
  3. **resolve_installation_id** — mints an App JWT (reusing
     :func:`github_app_auth.build_app_jwt`) and GETs ``/app/installations`` to find the
     installation for the target org, matching account login case-insensitively.
  4. **fetch_app_client_id** — same App JWT, GETs ``/app`` for the App's ``client_id``. The
     non-secret half of the OAuth pair IS recoverable this way for Apps onboarded before (2);
     the secret is not, and must be pasted once by an org admin.

SECURITY (trust boundary): the private key, the webhook secret, the ``client_secret``, and the
manifest ``code`` are NEVER logged and NEVER folded into an exception message (nor into any
``reason``). On failure the error carries only a fixed, safe string + the HTTP status code —
never the response body, which can echo attacker-controlled or credential-adjacent content.
``raise … from None`` drops the chained transport exception (its args can carry URLs with auth
material).

DETERMINISM: there is NO wall-clock read in this module. The caller injects ``now_epoch``
(Unix seconds) so the App JWT used for the installation lookup is reproducible in tests —
the repo forbids inline ``time.time()``/``datetime.now()`` in logic.
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import quote

import httpx

from services.github_app_auth import build_app_jwt

GITHUB_DEFAULT_BASE = "https://api.github.com"

# Least-privilege permission set the manifest requests.
# administration=write creates repos in the org; contents=write pushes scaffold files;
# workflows=write is REQUIRED to push the scaffold's .github/workflows/build.yml (writing a
# workflow FILE is gated separately from contents — a git/trees call containing a workflow path
# 403s "Resource not accessible by integration" without it); actions=write manages Actions runs;
# actions_variables=write sets the build-only CI repo vars (github_repo_service.set_ci_vars
# → POST /repos/.../actions/variables); metadata=read is the mandatory baseline every App holds.
#
# KEY NAME (2026-07-08): the repo "Variables" permission's machine key is "actions_variables",
# NOT "variables". Confirmed authoritatively via GET /app on the live agp-agenticops-platform App
# (which has Variables granted) — it reports "actions_variables": "write". An earlier attempt used
# "variables" and GitHub's manifest validator rejected the WHOLE manifest ("Default permission
# records resource is not included in the list") — that was the wrong spelling, not a missing
# capability. NOTE: GitHub does NOT retro-apply a manifest permission change to already-created
# Apps — existing installs still need Variables added by hand + a re-approval; this only helps
# freshly-onboarded orgs. If a fresh manifest onboarding 400s on this key, the validator lags the
# REST vocabulary again — drop it here and fall back to the manual grant.
#
# COVERAGE AUDIT (2026-07-13, re-checked E28B/T7 2026-08-02): every endpoint
# github_repo_service.py still calls, cross-checked against GitHub's authoritative
# "Permissions required for fine-grained personal access tokens" page (docs.github.com/en/rest/
# authentication/permissions-required-for-fine-grained-personal-access-tokens), whose per-endpoint
# rows are grouped under the exact App permission each requires. Result: EVERY endpoint is
# already covered by the set below — no permission added:
#   - POST /orgs/{org}/repos            (create_repo)          → Administration: write [have]
#   - PATCH /repos/{org}/{repo}         (set_default_branch)   → Administration: write [have]
#   - DELETE /repos/{org}/{repo}        (delete_repo)          → Administration: write [have]
#   - DELETE /repos/.../git/refs/heads/*(delete_branch)        → Contents: write      [have]
#   - git blobs/trees/commits/refs      (commit_files,
#                                        create_repo_from_zip) → Contents: write (+Workflows) [have]
#   - GET  /repos/{org}/{repo}          (repo_exists)          → Metadata: read      [have]
#   - GET  /repos/.../pulls/{number}    (read_pr)              → Pull requests: read (metadata:read
#                                        suffices for the READ this seam does)          [have]
#   - POST/PATCH /repos/.../actions/variables (set_ci_vars)    → Variables: write     [have]
# CRITICAL FINDING — a repo PATCH is Administration, NOT Metadata. `set_default_branch` PATCHes
# /repos/{org}/{repo}, which GitHub documents under Repository "Administration" (write) — already
# held. metadata:read is sufficient for the *reads* above. Bumping metadata read→write is therefore
# NOT required and would break least-privilege — do not add it.
#
# E28B/T7 — FOUR ROWS WERE REMOVED, AND NO PERMISSION WITH THEM. The endpoints behind
# `list_template_repos` (GET /orgs/{org}/repos), `generate_from_template` (POST /.../generate),
# `set_repo_metadata` (PATCH description/is_template + PUT /topics) and `commit_file`
# (PUT/GET /contents/{path}) are no longer called by any code path — the methods are deleted.
# Every permission they needed is STILL REQUIRED by a surviving row above, so the set below is
# unchanged and MUST NOT be narrowed on the strength of these deletions: administration:write
# still creates and deletes repos and moves the default branch, contents:write still carries the
# whole-template commit, metadata:read is the mandatory baseline regardless.
#
# DELIBERATELY ABSENT — pull_requests (E27B decision D7, 2026-07-29). E27C opens/approves/merges
# PRs on a human's behalf and will need it; E27B exercises no PR verb, so adding it here now buys
# nothing and risks the whole onboarding path. A WRONG machine key makes GitHub reject the ENTIRE
# manifest ("Default permission records resource is not included in the list") — see the
# actions_variables lesson above, where "variables" broke every fresh org onboarding — and the
# spelling is unverifiable offline. Since GitHub does NOT retro-apply a manifest permission change
# to already-created Apps, E27C needs a manual grant + re-approval on existing installs regardless.
# E27C MUST verify the key against a live `GET /app` (which reports the granted machine keys) on an
# App that has Pull requests granted, exactly as actions_variables was confirmed, BEFORE adding it.
MANIFEST_PERMISSIONS: dict[str, str] = {
    "administration": "write",
    "contents": "write",
    "workflows": "write",
    "actions": "write",
    "actions_variables": "write",
    "metadata": "read",
}


class GitHubManifestError(Exception):
    """A GitHub App manifest step failed. Carries a SAFE message only — never the
    private key, the webhook secret, the manifest code, or the response body."""


# GitHub caps App names at 34 characters (rejects the manifest otherwise).
_APP_NAME_MAX = 34


def build_app_name(org: str) -> str:
    """Build the GitHub App display name for ``org``, within GitHub's 34-char cap.

    ``agp-{org}`` — the ``agp-`` prefix namespaces AGP-managed Apps; the org login (globally
    unique on GitHub) keeps the name unique per org. For orgs long enough to exceed the 34-char
    cap the name is truncated (the App ID, not the name, is what AGP actually keys on)."""
    return f"agp-{org}"[:_APP_NAME_MAX]


def build_manifest(
    org: str,
    redirect_url: str,
    *,
    callback_urls: Optional[list[str]] = None,
) -> dict:
    """Build the GitHub App manifest JSON for ``org``.

    The App is org-owned (``public=False``). ``url`` (the App homepage, a GitHub-required
    field) and ``redirect_url`` both point at the AGP callback so GitHub returns the one-time
    conversion ``code`` there. No network call — pure data.

    TWO DIFFERENT REDIRECT SLOTS — do not conflate them:

      * ``redirect_url`` is where the **org admin** lands after *creating* the App (it carries
        the one-time manifest conversion ``code`` back to AGP).
      * ``callback_urls`` (E27B) is where an **end user** lands after *authorizing* the App in
        the user-to-server OAuth flow. GitHub allows up to 10 entries. It is set at creation
        time only — on an already-created App it is org-admin UI-only — which is why the
        manifest must carry it. Supplied ⇒ the key appears; omitted ⇒ the key is absent
        entirely (an empty array is a distinct input GitHub would treat as "no callbacks").

    NOTE: ``hook_attributes`` is intentionally OMITTED. GitHub's manifest schema makes
    ``hook_attributes.url`` mandatory whenever the ``hook_attributes`` object is present and
    otherwise rejects the manifest with a (misleadingly-worded) ``"url" wasn't supplied``. AGP
    does not consume App webhooks, so we omit the object entirely rather than invent a webhook
    URL — the App is created webhook-inactive by default.
    """
    manifest: dict = {
        "name": build_app_name(org),
        "url": redirect_url,
        "redirect_url": redirect_url,
        "public": False,
        "default_permissions": MANIFEST_PERMISSIONS,
    }
    if callback_urls:
        manifest["callback_urls"] = list(callback_urls)
    return manifest


def register_url(org: str, state: str) -> str:
    """Build the org-scoped GitHub URL that shows the admin the "Create App" screen.

    ``state`` is percent-encoded (CSRF token) so it round-trips safely through the URL.
    Org-scoped (``/organizations/{org}/...``) so the App is created under the org, not the
    admin's personal account.
    """
    return f"https://github.com/organizations/{org}/settings/apps/new?state={quote(state)}"


def convert_manifest_code(
    code: str,
    *,
    client: httpx.Client,
    base_url: Optional[str],
) -> dict:
    """Exchange a one-time manifest ``code`` for the new App's credentials.

    POSTs to ``{base}/app-manifests/{code}/conversions``. On 201 GitHub returns the App's
    ``id``, ``slug``, ``pem`` (private key), ``webhook_secret``, ``client_id`` and
    ``client_secret`` — the OAuth client pair being what makes per-user (user-to-server)
    GitHub auth possible at all (E27B). Returns all six as a dict with ``app_id`` stringified.
    ``base`` defaults to :data:`GITHUB_DEFAULT_BASE`; pass ``base_url`` for GitHub Enterprise.
    ``client`` is injectable so tests back it with ``httpx.MockTransport`` (no live GitHub call).

    DEFENSIVE ON THE OAUTH PAIR: GitHub's 201 schema (an ``allOf``) marks ``client_id`` and
    ``client_secret`` **required**, but its narrative page contradicts that schema — the prose
    lists only ``id``/``pem``/``webhook_secret`` — and GitHub publishes NO example response body
    for this endpoint. So both are read with ``.get`` and are deliberately NOT part of the
    completeness guard below: a schema surprise must degrade to "user linking unavailable for
    this connection", never break App onboarding, which works today. Only ``id``/``pem`` are
    load-bearing enough to hard-fail on.

    Raises :class:`GitHubManifestError` on any non-201 response, transport failure, or a
    response missing ``id``/``pem``. The error message is SAFE: it never contains the
    credentials (private key, webhook secret, ``client_secret``), the ``code``, or the response
    body — only a fixed string plus (for HTTP failures) the status code.
    """
    base = (base_url or GITHUB_DEFAULT_BASE).rstrip("/")
    url = f"{base}/app-manifests/{code}/conversions"
    headers = {"Accept": "application/vnd.github+json"}
    try:
        resp = client.post(url, headers=headers)
    except httpx.HTTPError as exc:
        # Never surface the exception value (URLs can carry the code) — type name only.
        raise GitHubManifestError(
            f"could not reach GitHub to convert the manifest ({type(exc).__name__})"
        ) from None

    if resp.status_code != 201:
        # Body is NEVER included — it can echo credential-adjacent content.
        raise GitHubManifestError(
            f"GitHub declined the manifest conversion (HTTP {resp.status_code})"
        )

    try:
        body = resp.json()
    except ValueError:
        body = {}
    app_id = body.get("id")
    pem = body.get("pem")
    if not app_id or not pem:
        raise GitHubManifestError("GitHub returned an incomplete manifest conversion")
    return {
        "app_id": str(app_id),
        "pem": pem,
        "webhook_secret": body.get("webhook_secret"),
        "slug": body.get("slug"),
        # E27B: the OAuth client pair. `.get` on purpose — see the docstring's defensive note.
        "client_id": body.get("client_id"),
        "client_secret": body.get("client_secret"),
    }


def resolve_installation_id(
    app_id: str,
    private_key_pem: str,
    org: str,
    *,
    client: httpx.Client,
    base_url: Optional[str],
    now_epoch: int,
) -> Optional[str]:
    """Resolve the installation id of the App for ``org``, or ``None`` if not installed.

    Mints an App JWT (:func:`github_app_auth.build_app_jwt`) and GETs
    ``{base}/app/installations`` with ``Authorization: Bearer {app_jwt}`` +
    ``Accept: application/vnd.github+json``, returning ``str(item["id"])`` for the first
    installation whose account login matches ``org`` case-insensitively.

    ``base`` defaults to :data:`GITHUB_DEFAULT_BASE`; pass ``base_url`` for GitHub
    Enterprise. ``client`` is injectable for tests; ``now_epoch`` flows through to the JWT
    for determinism.

    Raises :class:`GitHubManifestError` on any non-200 response or transport failure. The
    error message is SAFE: it never contains the private key or the response body — only a
    fixed string plus (for HTTP failures) the status code.
    """
    base = (base_url or GITHUB_DEFAULT_BASE).rstrip("/")
    app_jwt = build_app_jwt(app_id, private_key_pem, now_epoch=now_epoch)
    url = f"{base}/app/installations"
    headers = {
        "Authorization": f"Bearer {app_jwt}",
        "Accept": "application/vnd.github+json",
    }
    try:
        resp = client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        # Never surface the exception value (URLs can carry auth) — type name only.
        raise GitHubManifestError(
            f"could not reach GitHub for the installations lookup ({type(exc).__name__})"
        ) from None

    if resp.status_code != 200:
        # Body is NEVER included — it can echo credential-adjacent content.
        raise GitHubManifestError(
            f"GitHub declined the installations lookup (HTTP {resp.status_code})"
        )

    try:
        items = resp.json()
    except ValueError:
        items = []
    target = org.lower()
    for item in items or []:
        login = (item.get("account") or {}).get("login", "")
        if login.lower() == target:
            installation_id = item.get("id")
            if not installation_id:
                raise GitHubManifestError("GitHub returned an installation with no id")
            return str(installation_id)
    return None


def fetch_app_client_id(
    app_id: str,
    private_key_pem: str,
    *,
    client: httpx.Client,
    base_url: Optional[str],
    now_epoch: int,
) -> str:
    """Resolve the App's OAuth ``client_id`` from ``{base}/app`` (E27B).

    Mints an App JWT (:func:`github_app_auth.build_app_jwt`) and GETs ``{base}/app`` with
    ``Authorization: Bearer {app_jwt}`` + ``Accept: application/vnd.github+json``, returning
    ``str(body["client_id"])``.

    This is the recovery path for Apps onboarded before AGP captured the OAuth pair from the
    manifest conversion: the ``client_id`` is non-secret and readable here, while the matching
    ``client_secret`` has NO API at all and must be generated + pasted once by an org admin. It
    is also the verification seam for an admin-supplied pair — a pasted ``client_id`` that does
    not match this value belongs to a different App.

    ``base`` defaults to :data:`GITHUB_DEFAULT_BASE`; pass ``base_url`` for GitHub Enterprise.
    ``client`` is injectable for tests; ``now_epoch`` flows through to the JWT for determinism.

    Raises :class:`GitHubManifestError` on any non-200 response, transport failure, or a
    response with no ``client_id``. The error message is SAFE: it never contains the private
    key, the client secret, or the response body — only a fixed string plus (for HTTP failures)
    the status code.
    """
    base = (base_url or GITHUB_DEFAULT_BASE).rstrip("/")
    app_jwt = build_app_jwt(app_id, private_key_pem, now_epoch=now_epoch)
    url = f"{base}/app"
    headers = {
        "Authorization": f"Bearer {app_jwt}",
        "Accept": "application/vnd.github+json",
    }
    try:
        resp = client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        # Never surface the exception value (URLs can carry auth) — type name only.
        raise GitHubManifestError(
            f"could not reach GitHub for the app lookup ({type(exc).__name__})"
        ) from None

    if resp.status_code != 200:
        # Body is NEVER included — it can echo credential-adjacent content.
        raise GitHubManifestError(
            f"GitHub declined the app lookup (HTTP {resp.status_code})"
        )

    try:
        body = resp.json()
    except ValueError:
        body = {}
    client_id = (body or {}).get("client_id")
    if not client_id:
        raise GitHubManifestError("GitHub returned an app with no client id")
    return str(client_id)
