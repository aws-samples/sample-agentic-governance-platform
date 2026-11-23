"""GitHub user access tokens through the web application flow: transport only.

Where :mod:`services.github_app_auth` authenticates as the App, this module authenticates as a
human: a user access token (``ghu_``) makes a platform-initiated GitHub write be attributed to that
person. User access tokens use the App's fine-grained permissions, so no ``scope`` is requested
anywhere here. Five pure functions over an injected ``httpx.Client``: the PKCE challenge and the
authorize URL (no network), the code exchange, the refresh (destructive and single-use: the new
pair replaces the old one, whose atomic write is the caller's job), the identity probe (``GET
/user`` for the numeric id, the stable join key, and the login label; ``email`` is never read) and
the grant revocation (which also deletes every token GitHub issued the App for that user).

GitHub's token endpoint answers HTTP 200 on failure with an ``error`` field, so success is "200 and
a non-empty ``access_token``", owned by :func:`_token_post` for both token legs.

The ``client_secret``, the ``code``, the ``code_verifier`` and every token stay out of logs and out
of exception messages. A :class:`GitHubOAuthError` carries a fixed string plus the HTTP status, or
the OAuth ``error`` name when it is one of GitHub's documented values (anything else collapses to a
fixed literal, so an unbounded or newline-bearing body value never reaches a message); a transport
failure is re-raised ``from None``. Provider values are type-checked rather than coerced, so no
``ValueError`` quoting a response body can escape. No wall clock is read here; lifetimes pass
through as the integers GitHub reported.
"""

from __future__ import annotations

import base64
import hashlib
from urllib.parse import quote, urlencode

import httpx

GITHUB_WEB_BASE = "https://github.com"
GITHUB_API_BASE = "https://api.github.com"

_BAD_GRANT_ERRORS = frozenset({"bad_verification_code", "bad_refresh_token"})
_KNOWN_ERRORS = frozenset(
    {
        "bad_verification_code",
        "bad_refresh_token",
        "unsupported_grant_type",
        "incorrect_client_credentials",
        "redirect_uri_mismatch",
        "bad_client_credentials",
        "application_suspended",
        "access_denied",
    }
)
_UNRECOGNIZED_ERROR = "an unrecognized error"


class GitHubOAuthError(Exception):
    """A GitHub user-OAuth step failed. The message is fixed text plus at most an HTTP status or
    a documented OAuth error name: never the ``client_secret``, the ``code``, a token or a body.

    ``kind`` tells the caller how to react: ``provider_error`` (transport, HTTP or schema fault;
    retryable, link intact), ``revoked`` (GitHub answered 401: the human revoked the
    authorization, so the stored token is dead) or ``bad_grant`` (the ``code`` or
    ``refresh_token`` is spent, expired or invalid; re-run the web flow).
    """

    def __init__(self, message: str, kind: str = "provider_error") -> None:
        super().__init__(message)
        self.message = message
        self.kind = kind


def build_pkce_challenge(verifier: str) -> str:
    """``base64url(sha256(verifier))`` without padding: the 43-character S256 challenge, the only
    method GitHub supports. The caller generates and stores the verifier."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def build_authorize_url(
    *,
    client_id: str,
    redirect_uri: str,
    state: str,
    code_challenge: str,
) -> str:
    """The ``github.com/login/oauth/authorize`` URL: exactly ``client_id``, ``redirect_uri``,
    ``state``, ``code_challenge`` and ``code_challenge_method=S256``, every value percent-encoded,
    no ``scope``. ``redirect_uri`` must match a registered callback URL byte for byte."""
    query = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"{GITHUB_WEB_BASE}/login/oauth/authorize?{query}"


def _optional_int(value) -> int | None:
    """``value`` if it is a real ``int`` (``bool`` excluded), else ``None``: a lifetime the caller
    feeds to ``timedelta`` must be an int or absent."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _token_post(form: dict, *, client: httpx.Client, what: str) -> dict:
    """POST ``form`` to the token endpoint and normalize the response to the four keys
    ``access_token``, ``refresh_token``, ``expires_in`` and ``refresh_token_expires_in``; the
    last three are ``None`` when GitHub omits them (an App whose user tokens do not expire).

    Raises :class:`GitHubOAuthError` on a transport failure, a non-200 status, an ``error`` body
    or a missing or non-string ``access_token``.
    """
    url = f"{GITHUB_WEB_BASE}/login/oauth/access_token"
    headers = {"Accept": "application/json"}
    try:
        resp = client.post(url, data=form, headers=headers)
    except httpx.HTTPError as exc:
        raise GitHubOAuthError(f"could not reach GitHub to {what} ({type(exc).__name__})") from None

    if resp.status_code != 200:
        raise GitHubOAuthError(f"GitHub declined the {what} request (HTTP {resp.status_code})")

    try:
        body = resp.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}

    error = body.get("error")
    if error:
        safe = error if isinstance(error, str) and error in _KNOWN_ERRORS else _UNRECOGNIZED_ERROR
        kind = "bad_grant" if safe in _BAD_GRANT_ERRORS else "provider_error"
        raise GitHubOAuthError(f"GitHub rejected the {what} ({safe})", kind=kind)

    access_token = body.get("access_token")
    if not access_token or not isinstance(access_token, str):
        raise GitHubOAuthError(f"GitHub returned no access token for the {what}")

    refresh_token = body.get("refresh_token")
    return {
        "access_token": access_token,
        "refresh_token": refresh_token if isinstance(refresh_token, str) else None,
        "expires_in": _optional_int(body.get("expires_in")),
        "refresh_token_expires_in": _optional_int(body.get("refresh_token_expires_in")),
    }


def exchange_code(
    *,
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
    code_verifier: str,
    client: httpx.Client,
) -> dict:
    """Exchange a one-time authorization ``code`` for a user access token.

    ``redirect_uri`` must match the authorize call exactly and ``code_verifier`` is the PKCE
    secret whose challenge was sent there. Returns :func:`_token_post`'s normalized shape. Raises
    :class:`GitHubOAuthError`, ``kind="bad_grant"`` when the code is spent or expired.
    """
    form = {
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
    }
    return _token_post(form, client=client, what="code exchange")


def refresh_user_token(
    *,
    client_id: str,
    client_secret: str,
    refresh_token: str,
    client: httpx.Client,
) -> dict:
    """Trade a ``ghr_`` refresh token for a fresh user access token with ``grant_type=refresh_token``.

    Rotation is destructive and single-use: the old refresh token and the old access token stop
    working immediately, so the caller must persist the returned pair atomically. Returns
    :func:`_token_post`'s normalized shape. Raises :class:`GitHubOAuthError`, ``kind="bad_grant"``
    on ``bad_refresh_token``.
    """
    form = {
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    return _token_post(form, client=client, what="token refresh")


def fetch_user_identity(access_token: str, *, client: httpx.Client) -> dict:
    """``{"github_id": int, "github_login": str}`` from ``GET /user`` under ``access_token``.

    A 401 means the human revoked the App's authorization and raises ``kind="revoked"``, which is
    how revocation is discovered without the ``github_app_authorization`` webhook. Raises
    :class:`GitHubOAuthError` on any other non-200, a transport failure or a body whose ``id`` is
    not an int or whose ``login`` is not a string: a coerced value would carry body text out.
    """
    url = f"{GITHUB_API_BASE}/user"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.github+json",
    }
    try:
        resp = client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise GitHubOAuthError(
            f"could not reach GitHub for the user identity lookup ({type(exc).__name__})"
        ) from None

    if resp.status_code == 401:
        raise GitHubOAuthError("GitHub rejected the user token (HTTP 401)", kind="revoked")
    if resp.status_code != 200:
        raise GitHubOAuthError(f"GitHub declined the user identity lookup (HTTP {resp.status_code})")

    try:
        body = resp.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    github_id = body.get("id")
    github_login = body.get("login")
    if (
        not isinstance(github_id, int)
        or isinstance(github_id, bool)
        or not github_id
        or not isinstance(github_login, str)
        or not github_login
    ):
        raise GitHubOAuthError("GitHub returned an incomplete user identity")
    return {"github_id": github_id, "github_login": github_login}


def revoke_grant(
    *,
    client_id: str,
    client_secret: str,
    access_token: str,
    client: httpx.Client,
) -> None:
    """Delete the human's authorization of the App at GitHub.

    ``DELETE {api}/applications/{client_id}/grant`` with Basic auth (an app-owner endpoint) and
    the access token in the body; GitHub deletes every token it issued the App for that user. 204
    is success and so are 404 and 422 (the grant is already gone, the desired end state). Any
    other status or a transport failure raises :class:`GitHubOAuthError`.
    """
    url = f"{GITHUB_API_BASE}/applications/{quote(client_id, safe='')}/grant"
    headers = {"Accept": "application/vnd.github+json"}
    try:
        resp = client.request(
            "DELETE",
            url,
            headers=headers,
            auth=(client_id, client_secret),
            json={"access_token": access_token},
        )
    except httpx.HTTPError as exc:
        raise GitHubOAuthError(
            f"could not reach GitHub to revoke the authorization ({type(exc).__name__})"
        ) from None

    if resp.status_code in (204, 404, 422):
        return
    raise GitHubOAuthError(f"GitHub declined the authorization revocation (HTTP {resp.status_code})")
