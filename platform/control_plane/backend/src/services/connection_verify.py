"""Provider verification for Git org Connections (E19).

Pure-ish token + org-access verification over an injectable ``httpx.Client``.
The caller supplies the client (tests inject one backed by ``httpx.MockTransport``;
the connection service builds a real one). This module owns the GitHub/GitLab
endpoint sequence and the identity/org-access outcome mapping (spec §4).

Two probes per provider: an identity check, then an org/group visibility check.
``Authorization: Bearer {token}`` is set on EACH request here — the caller's
client carries no pre-set auth. The response body is NEVER folded into
``reason`` (it can echo attacker-controlled or sensitive content).
"""

from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote

import httpx

from models.connection import Provider

GITHUB_DEFAULT_BASE = "https://api.github.com"
GITLAB_DEFAULT_BASE = "https://gitlab.com"


@dataclass
class VerifyResult:
    ok: bool
    account_login: Optional[str]
    reason: Optional[str]


def verify_connection(
    provider: Provider,
    org: str,
    base_url: Optional[str],
    token: str,
    *,
    client: httpx.Client,
    is_app: bool = False,
) -> VerifyResult:
    """Verify a credential authenticates AND can see ``org`` on ``provider``.

    ``is_app`` selects the GitHub probe: a PAT (``is_app=False``) is checked with the
    user-identity endpoint (``GET /user``); a GitHub App **installation token**
    (``is_app=True``) has NO user identity — ``GET /user`` returns 403 "Resource not
    accessible by integration" — so it is checked with ``GET /installation/repositories``,
    the canonical liveness probe for an installation token (200 ⇒ the token is valid and the
    App is installed). The installation↔org binding is already established upstream (the
    installation id was resolved by matching the org, or pasted by the operator, and the
    token was minted for exactly that installation), so the App path uses the single probe.

    Returns a ``VerifyResult``; transport failures and the documented HTTP outcomes are
    mapped to fixed reason strings (no response body ever leaks).
    """
    if base_url:
        base = base_url.rstrip("/")
    elif provider == Provider.GITLAB:
        base = GITLAB_DEFAULT_BASE
    else:
        base = GITHUB_DEFAULT_BASE

    headers = {"Authorization": f"Bearer {token}"}
    unreachable_reason = f"could not reach {provider.value} ('{base}')"

    # GitHub App installation token: single liveness probe (see docstring).
    if is_app:
        url = f"{base}/installation/repositories"
        try:
            resp = client.get(url, headers=headers)
        except httpx.HTTPError:
            return VerifyResult(ok=False, account_login=None, reason=unreachable_reason)
        if resp.status_code == 401:
            return VerifyResult(
                ok=False, account_login=None, reason="installation token did not authenticate"
            )
        if resp.status_code != 200:
            # 403/404/5xx: the App isn't installed, lost access, or GitHub is unreachable.
            return VerifyResult(
                ok=False,
                account_login=None,
                reason=f"App installation is not active for org '{org}'",
            )
        # account_login = the org the App is installed on (the "account" for an App token).
        return VerifyResult(ok=True, account_login=org, reason=None)

    if provider == Provider.GITLAB:
        identity_url = f"{base}/api/v4/user"
        org_url = f"{base}/api/v4/groups/{quote(org, safe='')}"
        login_field = "username"
    else:
        identity_url = f"{base}/user"
        org_url = f"{base}/orgs/{org}"
        login_field = "login"

    try:
        identity_resp = client.get(identity_url, headers=headers)
        if identity_resp.status_code == 401:
            return VerifyResult(ok=False, account_login=None, reason="token did not authenticate")
        # Spec §4: success requires BOTH calls to return 200. Any other identity
        # status (403/429/5xx/...) is the network/other bucket, not success.
        if identity_resp.status_code != 200:
            return VerifyResult(ok=False, account_login=None, reason=unreachable_reason)

        # A malformed/non-JSON or non-object body (e.g. a 5xx HTML page slipping
        # through as 200) must NOT crash the caller and must NEVER leak into reason.
        try:
            identity_body = identity_resp.json()
        except ValueError:
            return VerifyResult(ok=False, account_login=None, reason=unreachable_reason)
        if not isinstance(identity_body, dict):
            return VerifyResult(ok=False, account_login=None, reason=unreachable_reason)
        account_login = identity_body.get(login_field)

        org_resp = client.get(org_url, headers=headers)
        if org_resp.status_code in (403, 404):
            return VerifyResult(
                ok=False,
                account_login=account_login,
                reason=f"token valid but org/group '{org}' is not visible to it",
            )
        if org_resp.status_code != 200:
            return VerifyResult(ok=False, account_login=account_login, reason=unreachable_reason)

        return VerifyResult(ok=True, account_login=account_login, reason=None)
    except httpx.HTTPError:
        return VerifyResult(
            ok=False,
            account_login=None,
            reason=unreachable_reason,
        )
