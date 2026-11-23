"""Per-org GitHub-OIDC ECR-push role lifecycle.

Each connected GitHub org gets its own IAM role that repos in that org, and only that org, may
assume through GitHub Actions OIDC to push the built agent image to the shared agent-images ECR
repository. The role's trust policy pins the OIDC ``sub`` to this org's repos in both shapes GitHub
mints, ``repo:<org>/*:*`` and the immutable-ID form ``repo:<org>@*/*:*``, so a repo in org A cannot
assume org B's role and a sibling login (``<org>-evil``) never prefix-matches. One shared role would
not do: the role ARN travels in every materialized repo's ``AWS_ECR_PUSH_ROLE_ARN`` Actions variable,
and a ``repo:*/*:*`` trust would let any GitHub repo that learns the ARN push to the registry.

The lifecycle follows the connection's: connect ensures the org's role (an idempotent
create-or-update), disconnect deletes it when no other connection still uses the org. The shared
platform-default role (``ensure_shared_role``) lives here too, under the name Terraform once gave it
so an existing deployment's live role is adopted rather than duplicated; it is the value stamped onto
repos whose connection has no per-org role, and it is never deleted. Every operation is idempotent,
and the ECS task role scopes IAM to ``role/<prefix>-ecr-push-*`` and ``role/<prefix>-agent-ecr-push``,
so this service cannot touch any other role.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Optional

from botocore.exceptions import ClientError

from core import aws_clients
from models.connection import ORG_LOGIN_RE

logger = logging.getLogger(__name__)

_INLINE_POLICY_NAME = "ecr-push"
# IAM RoleName: [\w+=,.@-]+, at most 64 characters
# (https://docs.aws.amazon.com/IAM/latest/APIReference/API_CreateRole.html).
_ROLE_NAME_MAX = 64
_SANITIZE = re.compile(r"[^A-Za-z0-9_=,.@-]")


class EcrPushRoleError(Exception):
    """A per-org ECR-push role operation failed. The message is a fixed literal of this module
    plus the org login, never a secret or an IAM response. ``kind`` is ``bad_request`` for an org
    that would widen the trust pattern it is interpolated into and ``iam_error`` for an IAM call
    that failed. No route lets it escape: the connection service's bootstrap path swallows and logs
    both, so it stays a plain exception and the kind is what a log reader gets."""

    def __init__(self, message: str, kind: str = "iam_error") -> None:
        super().__init__(message)
        self.message = message
        self.kind = kind


class EcrPushRoleService:
    """Create and delete the per-org GitHub-OIDC ECR-push role.

    ``role_name_prefix`` plus a sanitized org is the role name; ``oidc_provider_arn`` is the
    account's ``token.actions.githubusercontent.com`` provider; ``ecr_repository_arn`` is the shared
    agent-images repository the role may push to. When any is empty the service is inert:
    ``ensure_role`` and ``delete_role`` no-op and return ``None``, so a partially configured
    environment never blocks a connection."""

    def __init__(
        self,
        *,
        role_name_prefix: str,
        oidc_provider_arn: str,
        ecr_repository_arn: str,
        region: str | None = None,
        iam_client=None,
    ) -> None:
        self._prefix = role_name_prefix
        self._oidc_provider_arn = oidc_provider_arn
        self._ecr_repository_arn = ecr_repository_arn
        self.region = region
        self._iam = iam_client or aws_clients.client("iam", region=region)

    @property
    def enabled(self) -> bool:
        """All three wiring values must be present for the service to act."""
        return bool(self._prefix and self._oidc_provider_arn and self._ecr_repository_arn)

    def role_name(self, org: str) -> str:
        """``<prefix>-ecr-push-<sanitized-org>``, bounded to IAM's 64-character limit."""
        safe_org = _SANITIZE.sub("-", org)
        name = f"{self._prefix}-ecr-push-{safe_org}"
        return name[:_ROLE_NAME_MAX]

    def role_arn(self, account_id: str, org: str) -> str:
        return f"arn:aws:iam::{account_id}:role/{self.role_name(org)}"

    def shared_role_name(self) -> str:
        """``<prefix>-agent-ecr-push``, the platform-default fallback role's name: the one the
        retired Terraform resource produced, kept identical so ``ensure_shared_role`` adopts an
        existing deployment's live role instead of standing up a second one beside it."""
        return f"{self._prefix}-agent-ecr-push"

    @staticmethod
    def _require_valid_org(org: str) -> None:
        """Refuse an org that could widen the trust pattern it is about to be interpolated into.

        ``bad_request`` rather than ``iam_error``: nothing is wrong with IAM, the caller passed a
        value that must never reach a trust document. The model validator is the primary gate; this
        layer exists so a caller that never built a model cannot bypass it, and it fails loud rather
        than sanitizing, since turning ``*`` into ``-`` would mint a role trusting an org the
        operator never named."""
        if not ORG_LOGIN_RE.fullmatch(org or ""):
            raise EcrPushRoleError("invalid org for an OIDC trust policy", kind="bad_request")

    def _trust_policy(self, org: str) -> str:
        """The trust document of every role this service creates, so the org check in it covers
        the per-org and the shared role alike and a third caller cannot be added without it."""
        self._require_valid_org(org)
        return json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": {"Federated": self._oidc_provider_arn},
                        "Action": "sts:AssumeRoleWithWebIdentity",
                        "Condition": {
                            "StringEquals": {"token.actions.githubusercontent.com:aud": "sts.amazonaws.com"},
                            # GitHub's two sub shapes: the standard one and immutable-ID subject
                            # customization (https://docs.github.com/en/actions/security-for-github-actions/security-hardening-your-deployments/about-security-hardening-with-openid-connect#customizing-the-subject-claims-for-an-organization-or-repository).
                            "StringLike": {
                                "token.actions.githubusercontent.com:sub": [
                                    f"repo:{org}/*:*",
                                    f"repo:{org}@*/*:*",
                                ]
                            },
                        },
                    }
                ],
            }
        )

    def _permission_policy(self) -> str:
        return json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": "ecr:GetAuthorizationToken",
                        "Resource": "*",
                    },
                    {
                        "Effect": "Allow",
                        "Action": [
                            "ecr:BatchCheckLayerAvailability",
                            "ecr:InitiateLayerUpload",
                            "ecr:UploadLayerPart",
                            "ecr:CompleteLayerUpload",
                            "ecr:PutImage",
                            "ecr:BatchGetImage",
                            "ecr:GetDownloadUrlForLayer",
                        ],
                        "Resource": self._ecr_repository_arn,
                    },
                ],
            }
        )

    def ensure_role(self, org: str) -> Optional[str]:
        """Create or update the per-org role and return its ARN, ``None`` when the service is inert.
        Idempotent: an existing role has its trust and inline policy refreshed, so a re-connect
        converges. The trust is built before the IAM calls, so a refused org creates nothing."""
        if not self.enabled:
            logger.info("[ecr-push-role] service inert (unconfigured); skipping ensure for org")
            return None

        name = self.role_name(org)
        trust = self._trust_policy(org)
        try:
            try:
                self._iam.create_role(
                    RoleName=name,
                    AssumeRolePolicyDocument=trust,
                    Description=f"Per-org GitHub OIDC ECR-push role for org {org}",
                    Tags=[
                        {"Key": "ManagedBy", "Value": "agp"},
                        {"Key": "Purpose", "Value": "github-oidc-ecr-push"},
                    ],
                )
            except ClientError as err:
                if err.response.get("Error", {}).get("Code") == "EntityAlreadyExists":
                    self._iam.update_assume_role_policy(RoleName=name, PolicyDocument=trust)
                else:
                    raise
            self._iam.put_role_policy(
                RoleName=name,
                PolicyName=_INLINE_POLICY_NAME,
                PolicyDocument=self._permission_policy(),
            )
        except ClientError as err:
            logger.exception("[ecr-push-role] ensure_role failed for org %s", org)
            raise EcrPushRoleError(
                f"Failed to ensure ECR-push role for org '{org}'", kind="iam_error"
            ) from err

        arn = self._iam.get_role(RoleName=name)["Role"]["Arn"]
        logger.info("[ecr-push-role] ensured role %s for org %s", name, org)
        return arn

    def ensure_shared_role(self, org: str) -> Optional[str]:
        """Ensure the shared platform-default push role exists and return its ARN, ``None`` when
        inert.

        Adopt, do not re-trust: an existing role's trust policy is left exactly as found and only its
        inline permission policy is refreshed. The live role may be the one Terraform created, and
        GitHub Actions is actively using it; and the role is shared across every connected org while a
        trust names one, so refreshing it per connection would let the last connect win and revoke
        the previous org's fallback. That is safe because the per-org role wins at stamping:
        ``ProjectService.set_repo_vars`` prefers ``connection.ecr_push_role_arn``, so the shared role's
        single-org trust is reached only by a connection with no per-org role. ``org`` therefore only
        shapes the trust of a role that does not exist yet, the first connection on a fresh account.
        The trust is still built before the IAM calls, so a refused org creates nothing."""
        if not self.enabled:
            logger.info("[ecr-push-role] service inert (unconfigured); skipping shared ensure")
            return None

        name = self.shared_role_name()
        trust = self._trust_policy(org)
        try:
            try:
                self._iam.create_role(
                    RoleName=name,
                    AssumeRolePolicyDocument=trust,
                    Description="Shared platform-default GitHub OIDC ECR-push role",
                    Tags=[
                        {"Key": "ManagedBy", "Value": "agp"},
                        {"Key": "Purpose", "Value": "github-oidc-ecr-push"},
                    ],
                )
                logger.info("[ecr-push-role] created shared role %s", name)
            except ClientError as err:
                if err.response.get("Error", {}).get("Code") != "EntityAlreadyExists":
                    raise
            self._iam.put_role_policy(
                RoleName=name,
                PolicyName=_INLINE_POLICY_NAME,
                PolicyDocument=self._permission_policy(),
            )
        except ClientError as err:
            logger.exception("[ecr-push-role] ensure_shared_role failed for %s", name)
            raise EcrPushRoleError("Failed to ensure the shared ECR-push role", kind="iam_error") from err

        arn = self._iam.get_role(RoleName=name)["Role"]["Arn"]
        logger.info("[ecr-push-role] ensured shared role %s", name)
        return arn

    def delete_role(self, org: str) -> None:
        """Delete the per-org role and its inline policy, the policy first because a role with an
        attached policy cannot be deleted. Idempotent: a missing role or policy is a no-op, so a
        double disconnect or a never-provisioned org is safe. Inert when unconfigured."""
        if not self.enabled:
            return

        name = self.role_name(org)
        try:
            self._iam.delete_role_policy(RoleName=name, PolicyName=_INLINE_POLICY_NAME)
        except ClientError as err:
            if err.response.get("Error", {}).get("Code") != "NoSuchEntity":
                logger.exception("[ecr-push-role] delete inline policy failed for %s", name)
                raise EcrPushRoleError(
                    f"Failed to delete ECR-push role policy for org '{org}'", kind="iam_error"
                ) from err
        try:
            self._iam.delete_role(RoleName=name)
        except ClientError as err:
            if err.response.get("Error", {}).get("Code") == "NoSuchEntity":
                return
            logger.exception("[ecr-push-role] delete_role failed for %s", name)
            raise EcrPushRoleError(
                f"Failed to delete ECR-push role for org '{org}'", kind="iam_error"
            ) from err
        logger.info("[ecr-push-role] deleted role %s for org %s", name, org)
