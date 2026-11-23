"""GitHub App creation through the manifest flow.

AGP hands GitHub a pre-filled manifest, the org admin clicks "Create", and GitHub redirects back
with a one-time ``code`` that :func:`convert_manifest_code` exchanges for the App's credentials.
:func:`resolve_installation_id` and :func:`fetch_app_client_id` sign an App JWT and read the
installation for the org and the App's OAuth ``client_id``. :func:`build_manifest` and
:func:`register_url` touch no network.

The private key, the webhook secret, the ``client_secret``, the manifest ``code`` and every
response body stay out of logs and out of exception messages: a :class:`GitHubManifestError`
carries a fixed string plus, for an HTTP failure, the status code, and a transport failure is
re-raised ``from None`` because an ``httpx`` exception's args can carry a URL with auth material.
No wall clock is read here; the caller injects ``now_epoch``.
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import quote

import httpx

from services.github_app_auth import GitHubAppAuthError, build_app_jwt

GITHUB_DEFAULT_BASE = "https://api.github.com"

# The keys are GitHub's machine names ("actions_variables" is the repository Variables
# permission), and a key GitHub does not recognise makes it reject the whole manifest:
# https://docs.github.com/en/rest/authentication/permissions-required-for-fine-grained-personal-access-tokens
MANIFEST_PERMISSIONS: dict[str, str] = {
    "administration": "write",
    "contents": "write",
    "workflows": "write",
    "actions": "write",
    "actions_variables": "write",
    "metadata": "read",
}

_APP_NAME_MAX = 34


class GitHubManifestError(Exception):
    """A GitHub App manifest step failed. The message is fixed text plus at most an HTTP status
    code: never the private key, the webhook secret, the manifest code or a response body."""


def build_app_name(org: str) -> str:
    """``agp-{org}``, truncated to GitHub's 34-character App-name cap. The App id, not the
    name, is what AGP keys on."""
    return f"agp-{org}"[:_APP_NAME_MAX]


def build_manifest(
    org: str,
    redirect_url: str,
    *,
    callback_urls: Optional[list[str]] = None,
) -> dict:
    """The GitHub App manifest for ``org``: an org-owned, non-public App.

    ``redirect_url`` is where the org admin lands after creating the App, carrying the one-time
    conversion ``code``; it doubles as the App homepage ``url`` GitHub requires. ``callback_urls``
    is where an end user lands after authorizing the App in the user-to-server OAuth flow; GitHub
    accepts it at creation only, so the manifest must carry it. Omitted, the key is absent (an
    empty array is a distinct input).

    ``hook_attributes`` is omitted: GitHub makes ``hook_attributes.url`` mandatory whenever the
    object is present, and AGP consumes no App webhooks.
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
    """The org-scoped "Create App" URL, with the CSRF ``state`` percent-encoded."""
    return f"https://github.com/organizations/{org}/settings/apps/new?state={quote(state)}"


def convert_manifest_code(
    code: str,
    *,
    client: httpx.Client,
    base_url: Optional[str],
) -> dict:
    """Exchange a one-time manifest ``code`` for the new App's credentials.

    POSTs to ``{base}/app-manifests/{code}/conversions`` and returns ``app_id`` (stringified),
    ``pem``, ``webhook_secret``, ``slug``, ``client_id`` and ``client_secret``. Only ``id`` and
    ``pem`` are required: GitHub's schema marks the OAuth pair required while its prose omits it,
    so the pair is read with ``.get`` and its absence degrades to "user linking unavailable for
    this connection" rather than failing the onboarding.

    Raises :class:`GitHubManifestError` on a transport failure, a non-201 response or a body
    missing ``id`` or ``pem``.
    """
    base = (base_url or GITHUB_DEFAULT_BASE).rstrip("/")
    url = f"{base}/app-manifests/{code}/conversions"
    headers = {"Accept": "application/vnd.github+json"}
    try:
        resp = client.post(url, headers=headers)
    except (httpx.HTTPError, httpx.InvalidURL, UnicodeError) as exc:
        raise GitHubManifestError(
            f"could not reach GitHub to convert the manifest ({type(exc).__name__})"
        ) from None

    if resp.status_code != 201:
        raise GitHubManifestError(f"GitHub declined the manifest conversion (HTTP {resp.status_code})")

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
        "client_id": body.get("client_id"),
        "client_secret": body.get("client_secret"),
    }


def _app_jwt(app_id: str, private_key_pem: str, now_epoch: int) -> str:
    try:
        return build_app_jwt(app_id, private_key_pem, now_epoch=now_epoch)
    except GitHubAppAuthError as exc:
        raise GitHubManifestError(str(exc)) from None


def resolve_installation_id(
    app_id: str,
    private_key_pem: str,
    org: str,
    *,
    client: httpx.Client,
    base_url: Optional[str],
    now_epoch: int,
) -> Optional[str]:
    """The installation id of the App for ``org``, or ``None`` when it is not installed.

    GETs ``{base}/app/installations`` under an App JWT and returns ``str(item["id"])`` for the
    first installation whose account login matches ``org`` case-insensitively.

    Raises :class:`GitHubManifestError` on an unparseable private key, a transport failure, a
    non-200 response or an installation with no id.
    """
    base = (base_url or GITHUB_DEFAULT_BASE).rstrip("/")
    app_jwt = _app_jwt(app_id, private_key_pem, now_epoch)
    url = f"{base}/app/installations"
    headers = {
        "Authorization": f"Bearer {app_jwt}",
        "Accept": "application/vnd.github+json",
    }
    try:
        resp = client.get(url, headers=headers)
    except (httpx.HTTPError, httpx.InvalidURL, UnicodeError) as exc:
        raise GitHubManifestError(
            f"could not reach GitHub for the installations lookup ({type(exc).__name__})"
        ) from None

    if resp.status_code != 200:
        raise GitHubManifestError(f"GitHub declined the installations lookup (HTTP {resp.status_code})")

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
    """The App's OAuth ``client_id`` from ``{base}/app``, read under an App JWT.

    The recovery path for Apps onboarded before AGP captured the OAuth pair at conversion: the
    ``client_id`` is readable here, while the ``client_secret`` has no API and is pasted once by
    an org admin. A pasted ``client_id`` that differs from this value belongs to another App.

    Raises :class:`GitHubManifestError` on an unparseable private key, a transport failure, a
    non-200 response or a body with no ``client_id``.
    """
    base = (base_url or GITHUB_DEFAULT_BASE).rstrip("/")
    app_jwt = _app_jwt(app_id, private_key_pem, now_epoch)
    url = f"{base}/app"
    headers = {
        "Authorization": f"Bearer {app_jwt}",
        "Accept": "application/vnd.github+json",
    }
    try:
        resp = client.get(url, headers=headers)
    except (httpx.HTTPError, httpx.InvalidURL, UnicodeError) as exc:
        raise GitHubManifestError(
            f"could not reach GitHub for the app lookup ({type(exc).__name__})"
        ) from None

    if resp.status_code != 200:
        raise GitHubManifestError(f"GitHub declined the app lookup (HTTP {resp.status_code})")

    try:
        body = resp.json()
    except ValueError:
        body = {}
    client_id = (body or {}).get("client_id")
    if not client_id:
        raise GitHubManifestError("GitHub returned an app with no client id")
    return str(client_id)
