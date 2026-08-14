"""Account-global GitHub Actions OIDC provider bootstrap (platform capability, not IaC).

WHY THIS IS NOT TERRAFORM. A Git-provider integration is a PLATFORM capability: it comes
into existence when an operator connects an org, not when the stack is deployed. Terraform
therefore ships ZERO GitHub artifacts — a customer who never connects GitHub never gets a
GitHub dependency in their account, and the same pattern is what a future GitLab/Bitbucket
integration will follow (its provider/roles get bootstrapped by ITS connection path).

The provider is also the apply-time blocker that forced the move: every GitHub-OIDC role's
trust policy names it as its ``Federated`` principal, and IAM VALIDATES that principal
exists at role-create time. So on a provider-less account nothing GitHub-shaped can be
created by `terraform apply` at all — the provider has to exist first, and only the
connection path knows when that is.

Lifecycle is CREATE-ONLY and deliberately asymmetric (unlike the per-org roles in
``ecr_push_role_service``, which are torn down on disconnect):

  - connect (create / finalize) → ``ensure_provider()``  (idempotent get-or-create)
  - disconnect                  → NOTHING

There is no delete. The provider is ACCOUNT-GLOBAL: IAM permits exactly one per URL, and
anything else in the account that trusts GitHub Actions (other stacks, other teams, roles
AGP never created) breaks the moment it is removed. Deleting a shared singleton because
one of its consumers went away is not teardown, it is collateral damage — so the provider
is bootstrapped once and left in place.

The ARN is DETERMINISTIC (``oidc-provider/<url>``), which is what lets Terraform hand the
backend a plain string for the ECS env var without any resource existing yet.
"""

from __future__ import annotations

import logging
from typing import Optional

import boto3
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger(__name__)

# The GitHub Actions OIDC issuer. IAM keys the provider on this host (no scheme) and
# permits exactly ONE provider per host per account — hence get-or-create, never create.
GITHUB_OIDC_PROVIDER_HOST = "token.actions.githubusercontent.com"
GITHUB_OIDC_PROVIDER_URL = f"https://{GITHUB_OIDC_PROVIDER_HOST}"
# The audience GitHub Actions requests when assuming an AWS role via OIDC.
GITHUB_OIDC_CLIENT_IDS = ["sts.amazonaws.com"]
# AWS validates GitHub's OIDC certificate against its ROOT CA and IGNORES these
# thumbprints (they stopped matching GitHub's live leaf certificate long ago) — but
# ``CreateOpenIDConnectProvider`` still requires the field to be non-empty. They are
# therefore inert placeholders and never need rotation. Same two literals the retired
# Terraform resource carried, kept so an adopted provider and a created one look alike.
GITHUB_OIDC_THUMBPRINTS = [
    "6938fd4d98bab03faadb97b34396831e3780aea1",
    "1c58a3a8518e8759bf075b76b750d4f2df264fcd",
]


def github_oidc_provider_arn(account_id: str) -> str:
    """The account's GitHub-Actions OIDC provider ARN — DERIVED, never looked up.

    OIDC-provider ARNs are ``oidc-provider/<host>`` with no random component, so this is
    exact for an account that has the provider AND for one that does not yet. That is the
    property Terraform leans on: it can pass this string into the ECS task definition with
    no resource behind it, and the backend makes the object real on the first connection."""
    return f"arn:aws:iam::{account_id}:oidc-provider/{GITHUB_OIDC_PROVIDER_HOST}"


def resolve_github_oidc_provider_arn(
    configured: str, *, region: str = "us-east-1", sts_client=None
) -> str:
    """``configured`` (the ``GITHUB_OIDC_PROVIDER_ARN`` env) or the derived ARN.

    Terraform supplies the derived string, so the fallback is for envs that predate that
    (or a bare local shell): resolve the account via STS and derive it, so nothing else in
    the backend has to care whether the env var was wired. An STS failure returns ``""``,
    which leaves the dependent services INERT rather than raising at wiring time — the
    same "a partially-configured env never blocks a connection" rule they already follow."""
    if configured:
        return configured
    try:
        sts = sts_client or boto3.client("sts", region_name=region)
        account_id = sts.get_caller_identity()["Account"]
    except (ClientError, BotoCoreError, KeyError):
        logger.warning(
            "[github-oidc] no GITHUB_OIDC_PROVIDER_ARN and STS account lookup failed; "
            "GitHub-OIDC role provisioning stays inert"
        )
        return ""
    return github_oidc_provider_arn(account_id)


class GitHubOidcProviderError(Exception):
    """A provider bootstrap failed. Message is SAFE (no secret)."""

    def __init__(self, message: str, kind: str = "iam_error") -> None:
        super().__init__(message)
        self.message = message
        self.kind = kind


class GitHubOidcProviderService:
    """Get-or-create the account's GitHub Actions OIDC provider.

    ``provider_arn`` is the deterministic ARN (see :func:`github_oidc_provider_arn`); when
    it is empty the service is INERT — ``ensure_provider`` no-ops and returns None — so a
    partially-configured env never blocks a connection, matching ``EcrPushRoleService``."""

    def __init__(
        self,
        *,
        provider_arn: str,
        region: str = "us-east-1",
        iam_client=None,
    ) -> None:
        self._provider_arn = provider_arn
        self.region = region
        # Injectable for tests (moto / stub); lazily built otherwise. IAM is global but a
        # region keeps the client construction uniform with the other services.
        self._iam = iam_client or boto3.client("iam", region_name=region)

    # -- config gate --------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return bool(self._provider_arn)

    @property
    def provider_arn(self) -> str:
        return self._provider_arn

    # -- lifecycle ----------------------------------------------------------

    def _assert_audience(self, response) -> None:
        """Refuse a provider whose ``ClientIDList`` omits ``sts.amazonaws.com``.

        WHY THIS IS AN ASSERTION AND NOT A REPAIR. AGP does NOT hold
        ``iam:AddClientIDToOpenIDConnectProvider`` — deliberately, and it is the tightest
        thing in the grant: with it, a compromised backend could add an attacker-chosen
        ``aud`` to the account's GitHub trust anchor and mint tokens acceptable to EVERY
        role in the account that trusts GitHub Actions. So the service structurally cannot
        widen an adopted provider's audience, and detection is the whole remedy.

        WHY IT MATTERS ANYWAY. It is not a trust hole — the failure is fail-CLOSED: STS
        rejects every ``AssumeRoleWithWebIdentity`` whose token ``aud`` is not in the
        provider's client-ID list, so nothing extra is trusted and pushes simply stop.
        What it IS is the "record says success, reality disagrees" class this epic exists
        to delete: the bootstrap logged "provider ensured", the connection read CONNECTED,
        and then every GitHub Actions push failed with an opaque `Not authorized to perform
        sts:AssumeRoleWithWebIdentity` and nothing anywhere naming why.

        The response is ALREADY IN HAND on both paths (the GET's, and the re-GET after a
        race), so this costs no extra call. The message names the provider ARN and the
        missing audience because the fix is an operator action on that exact object
        (``aws iam add-client-id-to-open-id-connect-provider``), which AGP cannot perform."""
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
        """Ensure the provider exists AND accepts ``sts.amazonaws.com``; return its ARN
        (None if the service is inert).

        GET first, create only on ``NoSuchEntity`` — so the overwhelmingly common case (an
        account that already onboarded GitHub Actions, or a second connection) is one
        read-only call and touches nothing. A concurrent creator racing us to the same
        singleton surfaces as ``EntityAlreadyExists``, which IS the success condition:
        the object we wanted now exists, and its ARN is deterministic so it is the one we
        already hold.

        ADOPTION AND THE RACE ARE BOTH VERIFIED, never assumed. The ARN pins the ISSUER
        (IAM keys the provider on the host, so a successful GET on this exact ARN guarantees
        ``token.actions.githubusercontent.com`` — an attacker cannot substitute a different
        issuer under it). What the ARN does NOT pin is the AUDIENCE, and a provider some
        other stack created — or the racing winner created — may carry a ``ClientIDList``
        without ``sts.amazonaws.com``. Only the CREATE path knows its own audience by
        construction; the other two adopt someone else's object, so both re-read it. See
        :meth:`_assert_audience`."""
        if not self.enabled:
            logger.info("[github-oidc] service inert (unconfigured); skipping provider ensure")
            return None

        try:
            existing = self._iam.get_open_id_connect_provider(
                OpenIDConnectProviderArn=self._provider_arn
            )
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
                # Create-create race (two connections bootstrapping at once) — the
                # singleton exists, which is exactly what we asked for. But it is the
                # WINNER'S object, not ours, so its audience is re-read rather than
                # assumed: the racing creator may not be AGP at all.
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
        """Re-GET the raced provider so its audience can be checked.

        A read failure here is NOT folded into "audience missing" — that would report a
        wildly misleading cause (and an unactionable one) for what is really an IAM read
        problem, on a path where the object demonstrably exists. It raises as the read
        failure it is, matching the GET-path handling above."""
        try:
            return self._iam.get_open_id_connect_provider(
                OpenIDConnectProviderArn=self._provider_arn
            )
        except ClientError as err:
            logger.exception("[github-oidc] post-race provider lookup failed")
            raise GitHubOidcProviderError(
                "Failed to read the account's GitHub OIDC provider", kind="iam_error"
            ) from err
