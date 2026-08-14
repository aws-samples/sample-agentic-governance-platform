"""GitHub **user** access tokens via the web application flow (E27B/T1).

Where :mod:`services.github_app_auth` authenticates AS THE APP (installation tokens, a
single org-owned identity), this module authenticates AS A HUMAN. A *user access token*
(``ghu_...``) makes a platform-initiated GitHub write — open PR, approve PR, merge — be
attributed by GitHub to that person, not to AGP's App. GitHub's own words: "Unlike a
traditional OAuth token, the user access token does not use scopes. Instead, it uses
fine-grained permissions" — so there is deliberately **no ``scope`` parameter** anywhere
below, and "A token cannot grant additional access capabilities to a user."

Five deterministic seams, all pure functions over an injected ``httpx.Client``:

  1. **build_pkce_challenge / build_authorize_url** — no network; derive the S256 challenge
     and build the ``github.com/login/oauth/authorize`` URL the human's browser is sent to
     (``state`` carries CSRF protection, every value percent-encoded). GitHub supports only
     ``S256`` — "the ``plain`` code challenge method is not supported."
  2. **exchange_code** — POST the one-time ``code`` (+ ``code_verifier``) to
     ``/login/oauth/access_token`` for the ``ghu_``/``ghr_`` pair.
  3. **refresh_user_token** — same endpoint with ``grant_type=refresh_token``. Rotation is
     DESTRUCTIVE and single-use: the response's new ``refresh_token`` replaces the old one,
     and "that refresh token and the old user access token will no longer work." The atomic
     write of the rotated pair is the CALLER's problem — this module only transports.
  4. **fetch_user_identity** — GET ``/user`` for the numeric ``id`` (the stable join key)
     and ``login`` (a denormalized display label). ``email`` is never read: it is null-able
     and unusable as a key.
  5. **revoke_grant** — DELETE ``/applications/{client_id}/grant`` with Basic auth, which
     "will also delete all OAuth tokens associated with the application for the user," so an
     AGP unlink really kills the credential at GitHub instead of only locally.

⚠️ **GitHub's token endpoint answers HTTP 200 on failure**, with an ``error`` field in the
body instead of an error status. Success here is therefore "200 **AND** a non-empty
``access_token``" — never the status alone. :func:`_token_post` owns that discipline so both
token legs are forced through it.

This module holds NO storage, NO FastAPI, and NO principal: it is transport only, so the
composing service (:mod:`services.github_user_link`) owns state, secrets, and identity
binding.

SECURITY (trust boundary): the ``client_secret``, the authorization ``code``, the
``code_verifier``, and every access/refresh token are NEVER logged and NEVER folded into an
exception message. On failure the error carries only a fixed, safe string + the HTTP status
code — never the response body, which can echo attacker-controlled or credential-adjacent
content. The one exception is GitHub's OAuth ``error`` *name*, and only when it is a member of
the documented vocabulary :data:`_KNOWN_ERRORS` (``bad_verification_code``,
``bad_refresh_token``, …) — anything else collapses to a fixed literal, so an unbounded or
newline-bearing body value can never reach a message; ``error_description``/``error_uri`` are
always dropped. ``raise … from None`` drops the chained transport exception (its args can carry
URLs with auth material). Every provider value is TYPE-CHECKED before use rather than coerced:
a coercion (``int(...)``, ``str(...)``) can raise a bare ``ValueError``/``TypeError`` that
escapes :class:`GitHubOAuthError` **carrying body text in its own message**, so EVERY failure
in this module raises :class:`GitHubOAuthError` and nothing else.

DETERMINISM: there is NO wall-clock read in this module. Token lifetimes are passed straight
through as the integers GitHub reported (``expires_in``, ``refresh_token_expires_in``); the
caller turns them into absolute timestamps with ITS injected clock — the repo forbids inline
``time.time()``/``datetime.now()`` in logic.
"""

from __future__ import annotations

import base64
import hashlib
from urllib.parse import quote, urlencode

import httpx

GITHUB_WEB_BASE = "https://github.com"
GITHUB_API_BASE = "https://api.github.com"

# The OAuth `error` names that mean "this grant is dead — the human must re-authorize",
# as opposed to a transient/config provider fault.
_BAD_GRANT_ERRORS = frozenset({"bad_verification_code", "bad_refresh_token"})

# The FULL documented `error` vocabulary GitHub's token endpoint can answer with. Only a name
# from this set is ever echoed into an exception message: the body is provider-controlled, so an
# unrecognized value could be arbitrarily long or newline-bearing (log forging) — and a non-string
# one (`{"error": {...}}`) would blow up the membership test itself with an unhashable-type
# TypeError that escapes this module's "every failure is a GitHubOAuthError" contract.
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
    """A GitHub user-OAuth step failed. Carries a SAFE message only — never the
    ``client_secret``, the ``code``, a token, or the response body.

    ``kind`` tells the caller how to react:
      * ``"provider_error"`` — transport/HTTP/schema fault; retryable, link intact.
      * ``"revoked"`` — GitHub answered 401: the human revoked their authorization, so
        the stored token is dead and the link must be marked unlinked.
      * ``"bad_grant"`` — the ``code`` or ``refresh_token`` is spent/expired/invalid;
        re-run the web flow.
    """

    def __init__(self, message: str, kind: str = "provider_error") -> None:
        super().__init__(message)
        self.message = message
        self.kind = kind


def build_pkce_challenge(verifier: str) -> str:
    """Derive the S256 PKCE challenge for ``verifier``.

    ``base64url(sha256(verifier))`` with the ``=`` padding stripped — 43 characters, which
    is exactly what GitHub documents ("Must be a 43 character SHA-256 hash of a random
    string generated by the client"). S256 only; GitHub rejects the ``plain`` method.

    Pure and deterministic: the caller generates and stores the verifier (it is the secret
    half of the pair) and passes it in.
    """
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def build_authorize_url(
    *,
    client_id: str,
    redirect_uri: str,
    state: str,
    code_challenge: str,
) -> str:
    """Build the ``github.com/login/oauth/authorize`` URL the human's browser is sent to.

    Carries exactly five params — ``client_id``, ``redirect_uri``, ``state``,
    ``code_challenge``, ``code_challenge_method=S256`` — every value percent-encoded by
    :func:`urllib.parse.urlencode`. There is deliberately **no ``scope``**: a user access
    token uses the App's fine-grained permissions, and GitHub always returns an empty
    ``scope``.

    ``redirect_uri`` must be a byte-for-byte match for one of the App's registered Callback
    URLs and "can't contain any additional parameters"; ``state`` is the CSRF nonce the
    callback re-checks. No network call — pure data.
    """
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


def _optional_int(value) -> "int | None":
    """``value`` if it is a real ``int``, else ``None``.

    A provider-supplied lifetime the caller will feed to ``timedelta(seconds=...)`` must be an
    ``int`` or absent — a string or a dict would fail far from here, in the caller's arithmetic.
    ``bool`` is excluded on purpose: it is an ``int`` subclass and ``True`` seconds is nonsense.
    """
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _token_post(form: dict, *, client: httpx.Client, what: str) -> dict:
    """POST ``form`` to the token endpoint and normalize the response.

    Shared by :func:`exchange_code` and :func:`refresh_user_token` so the "HTTP 200 with an
    ``error`` field is still a FAILURE" trap is handled once and cannot drift between the
    two legs. Returns the four normalized keys; the three optional ones are ``None`` when
    absent, which is the documented shape for an App that does not expire user tokens
    (``expires_in``/``refresh_token``/``refresh_token_expires_in`` are simply omitted).

    Raises :class:`GitHubOAuthError` with a SAFE message on transport failure, a non-200
    status, an ``error`` body, or a missing ``access_token``.
    """
    url = f"{GITHUB_WEB_BASE}/login/oauth/access_token"
    headers = {"Accept": "application/json"}
    try:
        resp = client.post(url, data=form, headers=headers)
    except httpx.HTTPError as exc:
        # Never surface the exception value (URLs can carry auth) — type name only.
        raise GitHubOAuthError(
            f"could not reach GitHub to {what} ({type(exc).__name__})"
        ) from None

    if resp.status_code != 200:
        # Body is NEVER included — it can echo credential-adjacent content.
        raise GitHubOAuthError(f"GitHub declined the {what} request (HTTP {resp.status_code})")

    try:
        body = resp.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}

    error = body.get("error")
    if error:
        # Only a name from the DOCUMENTED vocabulary is echoed; anything else (a non-string, an
        # unknown name, an over-long or newline-bearing value) collapses to a fixed literal. The
        # isinstance check must come FIRST: a dict/list `error` would make the frozenset membership
        # test raise an unhashable-type TypeError, which would escape GitHubOAuthError entirely.
        # error_description/error_uri are dropped either way.
        safe = error if isinstance(error, str) and error in _KNOWN_ERRORS else _UNRECOGNIZED_ERROR
        kind = "bad_grant" if safe in _BAD_GRANT_ERRORS else "provider_error"
        raise GitHubOAuthError(f"GitHub rejected the {what} ({safe})", kind=kind)

    # Types are validated, not just truth-tested: a dict `access_token` passes `if not ...` and
    # would land in the caller's pinned `{"access_token": str}` secret body, and a string
    # `expires_in` would only fail later, inside the caller's timedelta arithmetic.
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

    POSTs ``client_id``/``client_secret``/``code``/``redirect_uri``/``code_verifier`` as a
    form to ``{web}/login/oauth/access_token`` with ``Accept: application/json``.
    ``redirect_uri`` must match the authorize call exactly; ``code_verifier`` is the PKCE
    secret whose challenge was sent there. ``client`` is injectable so tests back it with
    ``httpx.MockTransport`` (no live GitHub call).

    Returns the normalized ``{"access_token", "refresh_token", "expires_in",
    "refresh_token_expires_in"}`` shape — the last three ``None`` when the App does not
    expire user tokens.

    Raises :class:`GitHubOAuthError`; ``kind="bad_grant"`` when the code is spent or
    expired (``bad_verification_code``). The message never contains the secret, the code,
    the verifier, the token, or the response body.
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
    """Trade a ``ghr_...`` refresh token for a fresh user access token.

    POSTs ``grant_type=refresh_token`` (the literal string — anything else earns
    ``unsupported_grant_type``) plus ``client_id``/``client_secret``/``refresh_token`` to the
    same token endpoint.

    ⚠️ Rotation is DESTRUCTIVE and single-use: the response's ``refresh_token`` supersedes
    the one passed in, and the old refresh token AND the old access token stop working
    immediately. The caller MUST persist the returned pair atomically — losing it
    permanently breaks the link and forces the human to re-authorize. This function only
    transports; it holds no state.

    Returns the same normalized four-key shape as :func:`exchange_code`. Raises
    :class:`GitHubOAuthError` with ``kind="bad_grant"`` on ``bad_refresh_token`` (expired
    6-month window or an already-rotated token). SAFE message only.
    """
    form = {
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    return _token_post(form, client=client, what="token refresh")


def fetch_user_identity(access_token: str, *, client: httpx.Client) -> dict:
    """Resolve which GitHub human a user access token belongs to.

    GETs ``{api}/user`` with ``Authorization: Bearer {access_token}`` +
    ``Accept: application/vnd.github+json`` and returns
    ``{"github_id": int, "github_login": str}``. The numeric ``id`` is the STABLE join key
    (a login can be renamed); ``login`` is only a denormalized display label, refreshed on
    every verify. ``email`` is NEVER read — it is null-able and unusable as a key.

    A 401 ("Bad credentials") means the human revoked the App's authorization, so this
    raises ``GitHubOAuthError(kind="revoked")`` — the caller marks the link unlinked and
    prompts a re-link. That 401-as-revocation signal is why this probe exists: AGP does not
    consume the ``github_app_authorization`` webhook.

    Raises :class:`GitHubOAuthError` on any other non-200, a transport failure, or a
    response missing ``id``/``login``. SAFE message only — never the token or the body.
    """
    url = f"{GITHUB_API_BASE}/user"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.github+json",
    }
    try:
        resp = client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        # Never surface the exception value (URLs can carry auth) — type name only.
        raise GitHubOAuthError(
            f"could not reach GitHub for the user identity lookup ({type(exc).__name__})"
        ) from None

    if resp.status_code == 401:
        raise GitHubOAuthError("GitHub rejected the user token (HTTP 401)", kind="revoked")
    if resp.status_code != 200:
        # Body is NEVER included — it can echo credential-adjacent content.
        raise GitHubOAuthError(
            f"GitHub declined the user identity lookup (HTTP {resp.status_code})"
        )

    try:
        body = resp.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    # VALIDATE, never coerce. `int(github_id)` on a non-numeric string raises a bare ValueError
    # whose message quotes the response body verbatim ("invalid literal for int() ... '<body>'"),
    # which both escapes this module's GitHubOAuthError contract (the caller's route would 500)
    # and leaks body text into whatever log catches it. `str(github_login)` on a dict likewise
    # turns provider junk into AGP's persisted display label. `bool` is an `int` subclass, so it
    # is excluded explicitly.
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

    DELETEs ``{api}/applications/{client_id}/grant`` with **Basic auth**
    (``client_id``:``client_secret`` — this is an app-owner endpoint, not a bearer one) and
    body ``{"access_token": ...}``. Per GitHub, this "will also delete all OAuth tokens
     associated with the application for the user", so the ``ghu_``/``ghr_`` pair really dies
    server-side rather than only in AGP's store — the reason unlink calls this BEFORE
    deleting the local secret and row.

    204 is success. 404 and 422 are ALSO success: the grant is already gone, which is the
    desired end state (an unlink must be idempotent, e.g. when the human already revoked it
    from their GitHub settings). Any other status, or a transport failure, raises
    :class:`GitHubOAuthError` with a SAFE message — status code only, never the body, the
    secret, or the token.
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
        # Never surface the exception value (URLs can carry auth) — type name only.
        raise GitHubOAuthError(
            f"could not reach GitHub to revoke the authorization ({type(exc).__name__})"
        ) from None

    if resp.status_code in (204, 404, 422):
        return
    # Body is NEVER included — it can echo credential-adjacent content.
    raise GitHubOAuthError(
        f"GitHub declined the authorization revocation (HTTP {resp.status_code})"
    )
