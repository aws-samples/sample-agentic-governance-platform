"""GitHub App authentication for org connections: the App JWT and the installation token.

A GitHub App authenticates in two hops. :func:`build_app_jwt` signs a short-lived RS256 JWT with
the App's private key (``iss`` is the App id), which authenticates as the App itself.
:func:`mint_installation_token` posts that JWT to ``/app/installations/{id}/access_tokens`` and
returns the org-scoped, short-lived ``ghs_`` bearer token that the verify probe and the write
client consume through the ordinary ``Authorization: Bearer`` header.

The private key, the minted token and every response body stay out of logs and out of exception
messages: a :class:`GitHubAppAuthError` carries a fixed string plus, for an HTTP failure, the
status code. No wall clock is read here; the caller injects ``now_epoch``.
"""

from __future__ import annotations

from typing import Optional

import httpx
import jwt
from jwt.exceptions import InvalidKeyError

GITHUB_DEFAULT_BASE = "https://api.github.com"

# GitHub caps an App JWT at ten minutes and refuses an ``iat`` ahead of its own clock:
# https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-json-web-token-jwt-for-a-github-app
_IAT_SKEW_SECONDS = 60
_EXP_SECONDS = 540


class GitHubAppAuthError(Exception):
    """A GitHub App auth step failed. The message is fixed text plus at most an HTTP status
    code: never the private key, the minted token or a response body."""


def build_app_jwt(app_id: str, private_key_pem: str, *, now_epoch: int) -> str:
    """The App JWT: ``iss=app_id``, ``iat=now_epoch-60``, ``exp=now_epoch+540``, signed RS256.

    Raises :class:`GitHubAppAuthError` when PyJWT cannot parse ``private_key_pem``; the message
    names no key material."""
    payload = {
        "iss": app_id,
        "iat": now_epoch - _IAT_SKEW_SECONDS,
        "exp": now_epoch + _EXP_SECONDS,
    }
    try:
        return jwt.encode(payload, private_key_pem, algorithm="RS256")
    except InvalidKeyError:
        raise GitHubAppAuthError("the App private key could not be parsed") from None


def mint_installation_token(
    app_id: str,
    installation_id: str,
    private_key_pem: str,
    *,
    client: httpx.Client,
    base_url: Optional[str],
    now_epoch: int,
) -> str:
    """Mint a fresh installation access token for ``installation_id``.

    Signs an App JWT and POSTs it to ``{base}/app/installations/{installation_id}/access_tokens``;
    returns the response ``token``. ``base_url`` selects a GitHub Enterprise host; ``client`` is
    injectable so tests back it with ``httpx.MockTransport``.

    Raises :class:`GitHubAppAuthError` on an unparseable key, a transport failure (the exception
    type only, never its value) or a non-201 response (the status code only, never the body).
    """
    base = (base_url or GITHUB_DEFAULT_BASE).rstrip("/")
    app_jwt = build_app_jwt(app_id, private_key_pem, now_epoch=now_epoch)
    url = f"{base}/app/installations/{installation_id}/access_tokens"
    headers = {
        "Authorization": f"Bearer {app_jwt}",
        "Accept": "application/vnd.github+json",
    }
    try:
        resp = client.post(url, headers=headers)
    except (httpx.HTTPError, httpx.InvalidURL, UnicodeError) as exc:
        raise GitHubAppAuthError(
            f"could not reach GitHub to mint installation token ({type(exc).__name__})"
        ) from None

    if resp.status_code != 201:
        raise GitHubAppAuthError(f"GitHub declined the installation token request (HTTP {resp.status_code})")

    try:
        token = resp.json().get("token")
    except ValueError:
        token = None
    if not token:
        raise GitHubAppAuthError("GitHub returned no installation token")
    return token
