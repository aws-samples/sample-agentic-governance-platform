"""Provider verification for Git org connections over an injectable ``httpx.Client``.

Two probes per provider: an identity check, then an org or group visibility check; success
requires both to answer 200. A GitHub App installation token has no user identity, so it is
checked with the single ``GET /installation/repositories`` liveness probe instead.
``Authorization: Bearer {token}`` is set on each request here; the caller's client carries no
auth. Transport failures and the documented HTTP outcomes map to fixed reason strings, and a
response body is never folded into a reason.
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


def _api_base(provider: Provider, base_url: Optional[str]) -> str:
    if base_url:
        return base_url.rstrip("/")
    if provider == Provider.GITLAB:
        return GITLAB_DEFAULT_BASE
    return GITHUB_DEFAULT_BASE


def _verify_app_installation(
    base: str, org: str, headers: dict, *, client: httpx.Client, unreachable_reason: str
) -> VerifyResult:
    try:
        resp = client.get(f"{base}/installation/repositories", headers=headers)
    except (httpx.HTTPError, httpx.InvalidURL, UnicodeError):
        return VerifyResult(ok=False, account_login=None, reason=unreachable_reason)
    if resp.status_code == 401:
        return VerifyResult(ok=False, account_login=None, reason="installation token did not authenticate")
    if resp.status_code != 200:
        return VerifyResult(
            ok=False,
            account_login=None,
            reason=f"App installation is not active for org '{org}'",
        )
    return VerifyResult(ok=True, account_login=org, reason=None)


def _verify_identity_and_org(
    provider: Provider, base: str, org: str, headers: dict, *, client: httpx.Client, unreachable_reason: str
) -> VerifyResult:
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
        if identity_resp.status_code != 200:
            return VerifyResult(ok=False, account_login=None, reason=unreachable_reason)

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
    except (httpx.HTTPError, httpx.InvalidURL, UnicodeError):
        return VerifyResult(ok=False, account_login=None, reason=unreachable_reason)


def verify_connection(
    provider: Provider,
    org: str,
    base_url: Optional[str],
    token: str,
    *,
    client: httpx.Client,
    is_app: bool = False,
) -> VerifyResult:
    """Verify that a credential authenticates and can see ``org`` on ``provider``.

    ``is_app`` selects the GitHub App installation-token probe (``GET /installation/repositories``,
    200 means the token is valid and the App is installed) instead of the PAT probes
    (``GET /user`` then the org or group lookup).
    """
    base = _api_base(provider, base_url)
    headers = {"Authorization": f"Bearer {token}"}
    unreachable_reason = f"could not reach {provider.value} ('{base}')"

    if is_app:
        return _verify_app_installation(
            base, org, headers, client=client, unreachable_reason=unreachable_reason
        )
    return _verify_identity_and_org(
        provider, base, org, headers, client=client, unreachable_reason=unreachable_reason
    )
