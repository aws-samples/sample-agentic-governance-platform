"""GitHub App authentication for org Connections (E20/T9).

A GitHub App authenticates in two hops, both of which this module owns:

  1. **App JWT** — a short-lived RS256 JWT signed with the App's PRIVATE KEY, whose
     ``iss`` is the App ID. It authenticates AS THE APP (not any installation).
  2. **Installation access token** — POST the App JWT to
     ``/app/installations/{installation_id}/access_tokens``; GitHub returns a fresh,
     org-scoped, short-lived (~1h) ``ghs_...`` token. That token is a plain
     ``Authorization: Bearer {token}`` credential — the SAME shape the verify probe
     (``connection_verify``) and the write client (``github_repo_service``) already
     consume. So App auth adds NO new client surface: mint here, hand the result to
     the existing token seam.

WHY App > PAT: the identity is org-owned (survives staff churn), permissions are
scoped to what the App was granted, and every minted token is short-lived — a much
smaller blast radius than a long-lived personal PAT.

SECURITY (trust boundary): the private key and every minted token are NEVER logged and
NEVER folded into an exception message or the ``reason`` of an error. On failure the
error carries only a fixed, safe string + the HTTP status code (never the response body,
which can echo attacker-controlled or credential-adjacent content).

DETERMINISM: there is NO wall-clock read in this module. The caller injects ``now_epoch``
(Unix seconds) so the JWT ``iat``/``exp`` are reproducible in tests — the repo forbids
inline ``time.time()``/``datetime.now()`` in logic.
"""

from __future__ import annotations

from typing import Optional

import httpx
import jwt

GITHUB_DEFAULT_BASE = "https://api.github.com"

# JWT time window (GitHub caps App JWTs at 10 minutes). Backdate ``iat`` 60s to absorb
# minor clock skew between AGP and GitHub; ``exp`` 9 minutes out stays inside the cap.
_IAT_SKEW_SECONDS = 60
_EXP_SECONDS = 540


class GitHubAppAuthError(Exception):
    """A GitHub App auth step failed. Carries a SAFE message only — never the private
    key, the minted token, or the response body."""


def build_app_jwt(app_id: str, private_key_pem: str, *, now_epoch: int) -> str:
    """Build a short-lived RS256 App JWT signed with the App's private key.

    Claims: ``iss=app_id``, ``iat=now_epoch-60`` (clock-skew safe), ``exp=now_epoch+540``
    (inside GitHub's 10-minute cap). ``now_epoch`` is injected (Unix seconds) for
    determinism — this function reads no clock.

    The returned JWT authenticates as the App itself; exchange it for an installation
    token via :func:`mint_installation_token`. The private key is used only to sign and
    is never logged.
    """
    payload = {
        "iss": app_id,
        "iat": now_epoch - _IAT_SKEW_SECONDS,
        "exp": now_epoch + _EXP_SECONDS,
    }
    return jwt.encode(payload, private_key_pem, algorithm="RS256")


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

    Signs an App JWT (:func:`build_app_jwt`), POSTs it to
    ``{base}/app/installations/{installation_id}/access_tokens`` with
    ``Authorization: Bearer {app_jwt}`` + ``Accept: application/vnd.github+json``, and
    returns the response ``token`` (a short-lived org-scoped ``ghs_...`` bearer token).

    ``base`` defaults to :data:`GITHUB_DEFAULT_BASE`; pass ``base_url`` for GitHub
    Enterprise. ``client`` is injectable so tests back it with ``httpx.MockTransport``
    (no live GitHub call). ``now_epoch`` flows through to the JWT for determinism.

    Raises :class:`GitHubAppAuthError` on any non-201 response or transport failure. The
    error message is SAFE: it never contains the private key, the minted token, or the
    response body — only a fixed string plus (for HTTP failures) the status code.
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
    except httpx.HTTPError as exc:
        # Never surface the exception value (URLs can carry auth) — type name only.
        raise GitHubAppAuthError(
            f"could not reach GitHub to mint installation token ({type(exc).__name__})"
        ) from None

    if resp.status_code != 201:
        # Body is NEVER included — it can echo credential-adjacent content.
        raise GitHubAppAuthError(
            f"GitHub declined the installation token request (HTTP {resp.status_code})"
        )

    try:
        token = resp.json().get("token")
    except ValueError:
        token = None
    if not token:
        raise GitHubAppAuthError("GitHub returned no installation token")
    return token
