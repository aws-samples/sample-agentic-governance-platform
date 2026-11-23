"""The account-global GitHub Actions OIDC provider, bootstrapped by the connection path.

A Git-provider integration is a platform capability: it comes into existence when an operator
connects an org, not when the stack is deployed, so Terraform ships no GitHub artifact and every
GitHub-OIDC role's ``Federated`` principal (which IAM validates at role-create time) is created
here first. The lifecycle is create-only: ``ensure_provider`` is an idempotent get-or-create and a
disconnect removes nothing, because IAM permits one provider per issuer URL per account and every
other stack that trusts GitHub Actions breaks the moment it is deleted. The ARN is deterministic
(``oidc-provider/<host>``), which is what lets Terraform hand the backend a plain string before
the object exists.
"""

from __future__ import annotations

import logging
from typing import Optional

from botocore.exceptions import BotoCoreError, ClientError

from core import aws_clients

logger = logging.getLogger(__name__)

GITHUB_OIDC_PROVIDER_HOST = "token.actions.githubusercontent.com"
GITHUB_OIDC_PROVIDER_URL = f"https://{GITHUB_OIDC_PROVIDER_HOST}"
GITHUB_OIDC_CLIENT_IDS = ["sts.amazonaws.com"]
# IAM validates GitHub's certificate against its root CA and ignores the thumbprints, but
# CreateOpenIDConnectProvider still requires a non-empty list:
# https://docs.aws.amazon.com/IAM/latest/UserGuide/id_roles_providers_create_oidc_verify-thumbprint.html
GITHUB_OIDC_THUMBPRINTS = [
    "6938fd4d98bab03faadb97b34396831e3780aea1",
    "1c58a3a8518e8759bf075b76b750d4f2df264fcd",
]


def github_oidc_provider_arn(account_id: str) -> str:
    """The account's GitHub Actions OIDC provider ARN, derived rather than looked up: an OIDC
    provider ARN is ``oidc-provider/<host>`` with no random component, so it is exact whether or
    not the provider exists yet."""
    return f"arn:aws:iam::{account_id}:oidc-provider/{GITHUB_OIDC_PROVIDER_HOST}"


def resolve_github_oidc_provider_arn(configured: str, *, region: str | None = None, sts_client=None) -> str:
    """``configured`` (the ``GITHUB_OIDC_PROVIDER_ARN`` env) or the ARN derived from the STS
    account. An STS failure returns ``""``, which leaves the dependent services inert rather than
    failing at wiring time."""
    if configured:
        return configured
    try:
        sts = sts_client or aws_clients.client("sts", region=region)
        account_id = sts.get_caller_identity()["Account"]
    except (ClientError, BotoCoreError, KeyError):
        logger.warning(
            "[github-oidc] no GITHUB_OIDC_PROVIDER_ARN and STS account lookup failed; "
            "GitHub-OIDC role provisioning stays inert"
        )
        return ""
    return github_oidc_provider_arn(account_id)


class GitHubOidcProviderError(Exception):
    """A provider bootstrap failed. The message names the provider and the remedy, never an
    IAM error body."""

    def __init__(self, message: str, kind: str = "iam_error") -> None:
        super().__init__(message)
        self.message = message
        self.kind = kind


class GitHubOidcProviderService:
    """Get-or-create the account's GitHub Actions OIDC provider.

    ``provider_arn`` is the deterministic ARN; when it is empty the service is inert and
    ``ensure_provider`` returns ``None``, so a partially configured env never blocks a connection.
    """

    def __init__(
        self,
        *,
        provider_arn: str,
        region: str | None = None,
        iam_client=None,
    ) -> None:
        self._provider_arn = provider_arn
        self.region = region
        self._iam = iam_client or aws_clients.client("iam", region=region)

    @property
    def enabled(self) -> bool:
        return bool(self._provider_arn)

    @property
    def provider_arn(self) -> str:
        return self._provider_arn

    def _assert_audience(self, response) -> None:
        """Refuse a provider whose ``ClientIDList`` omits ``sts.amazonaws.com``.

        AGP deliberately holds no ``iam:AddClientIDToOpenIDConnectProvider``, so it cannot widen
        an adopted provider's audience; detection is the whole remedy. The failure is closed (STS
        rejects every token whose ``aud`` is not listed), but without this check the bootstrap
        would log success while every GitHub Actions push failed on an opaque STS denial. The
        message names the provider ARN and the missing audience, the operator action AGP cannot
        perform."""
        client_ids = (response or {}).get("ClientIDList") or []
        missing = [a for a in GITHUB_OIDC_CLIENT_IDS if a not in client_ids]
        if not missing:
            return
        logger.error(
            "[github-oidc] provider %s is missing audience %s; every GitHub Actions "
            "AssumeRoleWithWebIdentity will be denied until it is added",
            self._provider_arn,
            ",".join(missing),
        )
        raise GitHubOidcProviderError(
            f"The account's GitHub OIDC provider ({self._provider_arn}) does not accept the "
            f"audience {','.join(missing)}, so GitHub Actions cannot assume any AGP push role. "
            "Add it to the provider's client-ID list (AGP deliberately cannot).",
            kind="iam_error",
        )

    def ensure_provider(self) -> Optional[str]:
        """Ensure the provider exists and accepts ``sts.amazonaws.com``; return its ARN, or
        ``None`` when the service is inert.

        GET first and create only on ``NoSuchEntity``, so the common case (an account that already
        onboarded GitHub Actions, or a second connection) is one read-only call. A concurrent
        creator surfaces as ``EntityAlreadyExists``, which is the success condition. The ARN pins
        the issuer but not the audience, so an adopted provider and a raced one are both re-read
        and checked through :meth:`_assert_audience`; only the create path knows its audience by
        construction."""
        if not self.enabled:
            logger.info("[github-oidc] service inert (unconfigured); skipping provider ensure")
            return None

        try:
            existing = self._iam.get_open_id_connect_provider(OpenIDConnectProviderArn=self._provider_arn)
            self._assert_audience(existing)
            return self._provider_arn
        except ClientError as err:
            if err.response.get("Error", {}).get("Code") != "NoSuchEntity":
                logger.exception("[github-oidc] provider lookup failed")
                raise GitHubOidcProviderError(
                    "Failed to read the account's GitHub OIDC provider", kind="iam_error"
                ) from err

        try:
            self._iam.create_open_id_connect_provider(
                Url=GITHUB_OIDC_PROVIDER_URL,
                ClientIDList=list(GITHUB_OIDC_CLIENT_IDS),
                ThumbprintList=list(GITHUB_OIDC_THUMBPRINTS),
                Tags=[
                    {"Key": "ManagedBy", "Value": "agp"},
                    {"Key": "Purpose", "Value": "github-actions-oidc"},
                ],
            )
        except ClientError as err:
            if err.response.get("Error", {}).get("Code") == "EntityAlreadyExists":
                logger.info("[github-oidc] provider already created concurrently; adopting")
                self._assert_audience(self._read_provider_after_race())
                return self._provider_arn
            logger.exception("[github-oidc] provider create failed")
            raise GitHubOidcProviderError(
                "Failed to create the account's GitHub OIDC provider", kind="iam_error"
            ) from err

        logger.info("[github-oidc] created account GitHub Actions OIDC provider")
        return self._provider_arn

    def _read_provider_after_race(self):
        """Re-GET the raced provider so its audience can be checked. A read failure is reported
        as the read failure it is, never folded into "audience missing"."""
        try:
            return self._iam.get_open_id_connect_provider(OpenIDConnectProviderArn=self._provider_arn)
        except ClientError as err:
            logger.exception("[github-oidc] post-race provider lookup failed")
            raise GitHubOidcProviderError(
                "Failed to read the account's GitHub OIDC provider", kind="iam_error"
            ) from err
