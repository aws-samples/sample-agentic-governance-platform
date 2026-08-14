"""Per-org GitHub-OIDC ECR-push role lifecycle (E22 multi-org bugfix).

Each connected GitHub org gets its OWN IAM role that repos in that org (and only that
org) may assume via GitHub Actions OIDC to push the built agent image to the shared
agent-images ECR repo. The role's trust policy pins the OIDC ``sub`` to THIS org's
repos — accepting BOTH ``repo:<org>/*:*`` (standard) and ``repo:<org>@*/*:*`` (the
immutable-ID subject-customization form, where the sub is ``repo:<org>@<orgId>/...``).
The ``@*`` sits between the org and the ``/`` so it matches only this org's immutable-id
subs and never prefix-matches a sibling login (``<org>-evil``) — so a repo in org A
cannot assume org B's role, and orgs with subject customization can still push.

Why per-org (not one shared role): the ECR-push role ARN travels in every materialized
repo's ``AWS_ECR_PUSH_ROLE_ARN`` Actions var, so a single ``repo:*/*:*`` trust would let
ANY GitHub repo that learns the ARN push to our ECR. Per-org roles keep the trust
boundary tight while still supporting dynamic org connections.

Lifecycle is SYMMETRIC and driven by the connection lifecycle:
  - connect (create / finalize)  → ``ensure_role(org)``  (idempotent create-or-update)
  - disconnect (delete)          → ``delete_role(org)``  (idempotent; caller guards on
                                    "no other connection still uses this org")

THE SHARED PLATFORM-DEFAULT ROLE also lives here (``ensure_shared_role``). It used to be a
Terraform resource (``modules/agent_ecr``), which stopped being possible once the GitHub
OIDC provider became a platform-bootstrapped object: the role's trust policy names the
provider as its ``Federated`` principal and IAM validates that principal EXISTS at
create-time, so on a provider-less account `terraform apply` could not create the role at
all. It is provisioned here instead, right after
``github_oidc_provider_service.ensure_provider()``, and it lives in THIS class rather than
its own because its trust and permission policies are byte-for-byte the ones below — a
separate service would be a copy of both. Its name is the one Terraform used
(``<prefix>-agent-ecr-push``) so an existing deployment's live role is ADOPTED, not
duplicated. Unlike the per-org roles it is never deleted: it is the fallback stamped onto
repos whose connection has no per-org role, so no single disconnect owns it.

All operations are idempotent so a ret[ried] connect / a re-connect / a double delete are
safe. IAM calls are scoped by the ECS task role to ``role/<prefix>-ecr-push-*`` and
``role/<prefix>-agent-ecr-push`` (see the ecs module's iam policy) — this service cannot
touch any other role.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Optional

import boto3
from botocore.exceptions import ClientError

from models.connection import ORG_LOGIN_RE

logger = logging.getLogger(__name__)

_INLINE_POLICY_NAME = "ecr-push"
# IAM role names allow [\w+=,.@-] and max 64 chars. Org logins are [A-Za-z0-9-] (GitHub
# rules), so they already fit, but sanitize defensively and bound the length.
_ROLE_NAME_MAX = 64
_SANITIZE = re.compile(r"[^A-Za-z0-9_=,.@-]")

# THE SAME pattern the write models enforce (``models.connection.ORG_LOGIN_RE``), re-checked
# HERE as defense in depth. Imported rather than re-declared so the two layers can never
# drift apart: a single regex, two enforcement points.
#
# Why both layers. ``_SANITIZE`` above protects the role NAME and is NOT a trust guard —
# it rewrites bad characters, which is exactly wrong for the trust document: silently
# turning `*` into `-` would mint a role trusting an org the operator never named. The
# trust policy interpolates the RAW org into a ``StringLike`` sub pattern where `*` and `?`
# are wildcards, so `org="*"` yields ``repo:*/*:*`` — the trust this module's docstring
# forbids, on a role whose ARN is published to third-party repos. The model validator is
# the primary gate; this one exists so a future caller that constructs a service directly
# (a script, a migration, a test helper, a new route that forgot the model) cannot bypass
# it. It FAILS LOUD rather than sanitizing. See ``_require_valid_org``.


class EcrPushRoleError(Exception):
    """A per-org ECR-push role operation failed. Message is SAFE (no secret)."""

    def __init__(self, message: str, kind: str = "iam_error") -> None:
        super().__init__(message)
        self.message = message
        self.kind = kind


class EcrPushRoleService:
    """Create/delete the per-org GitHub-OIDC ECR-push role.

    ``role_name_prefix`` + a sanitized org → the role name. ``oidc_provider_arn`` is the
    account's ``token.actions.githubusercontent.com`` provider; ``ecr_repository_arn`` is
    the shared agent-images repo the role may push to. All three are wired from Terraform
    (see config). When any is empty the service is INERT — ``ensure_role``/``delete_role``
    no-op and return None — so a partially-configured env never blocks a connection."""

    def __init__(
        self,
        *,
        role_name_prefix: str,
        oidc_provider_arn: str,
        ecr_repository_arn: str,
        region: str = "us-east-1",
        iam_client=None,
    ) -> None:
        self._prefix = role_name_prefix
        self._oidc_provider_arn = oidc_provider_arn
        self._ecr_repository_arn = ecr_repository_arn
        self.region = region
        # Injectable for tests (moto / stub); lazily built otherwise. IAM is global but a
        # region keeps the client construction uniform with the other services.
        self._iam = iam_client or boto3.client("iam", region_name=region)

    # -- config gate --------------------------------------------------------

    @property
    def enabled(self) -> bool:
        """All three wiring values must be present for the service to act."""
        return bool(self._prefix and self._oidc_provider_arn and self._ecr_repository_arn)

    # -- naming -------------------------------------------------------------

    def role_name(self, org: str) -> str:
        """``<prefix>-ecr-push-<sanitized-org>``, bounded to IAM's 64-char limit."""
        safe_org = _SANITIZE.sub("-", org)
        name = f"{self._prefix}-ecr-push-{safe_org}"
        return name[:_ROLE_NAME_MAX]

    def role_arn(self, account_id: str, org: str) -> str:
        return f"arn:aws:iam::{account_id}:role/{self.role_name(org)}"

    def shared_role_name(self) -> str:
        """``<prefix>-agent-ecr-push`` — the platform-default fallback role's name.

        This literal is NOT a new choice: it is the name the retired
        ``modules/agent_ecr`` Terraform resource produced (``"${var.name_prefix}-agent-ecr-push"``),
        and ``ECR_PUSH_ROLE_NAME_PREFIX`` is that same ``name_prefix``. Keeping it identical
        is what makes ``ensure_shared_role`` ADOPT an existing deployment's live role
        instead of standing up a second one beside it."""
        return f"{self._prefix}-agent-ecr-push"

    # -- trust + permission policies ---------------------------------------

    @staticmethod
    def _require_valid_org(org: str) -> None:
        """Refuse an org that could widen the trust pattern it is about to be interpolated
        into.

        ``kind="bad_request"`` rather than the class default ``"iam_error"``: nothing is
        wrong with IAM here, the CALLER passed a value that must never reach a trust
        document, and the distinction is what a log reader (or a future route that does
        surface this) needs. Note that today no route sees it —
        ``ConnectionService._ensure_ecr_push_role`` swallows every exception by design (an
        IAM bootstrap must not fail an operator's credential handshake), so on the wired path
        this layer's job is to STOP THE IAM CALL, and the model validator is what produces
        the 422 the operator reads. That is precisely why both layers exist rather than only
        this one."""
        # fullmatch, not match — see models/connection.py: match()'s `$` tolerates a
        # trailing newline, which would land verbatim in the IAM trust policy.
        if not ORG_LOGIN_RE.fullmatch(org or ""):
            raise EcrPushRoleError(
                "invalid org for an OIDC trust policy", kind="bad_request"
            )

    def _trust_policy(self, org: str) -> str:
        # BOTH ``ensure_role`` and ``ensure_shared_role`` build their trust here, so this one
        # check covers every role this service can create. It is the LAST line before the
        # value becomes an IAM wildcard, and it must stay in this method (not in the two
        # callers) so a third one cannot be added without it.
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
                            "StringEquals": {
                                "token.actions.githubusercontent.com:aud": "sts.amazonaws.com"
                            },
                            # Pin to THIS org's repos only. Two sub shapes are trusted:
                            #   repo:<org>/*:*      — standard GitHub OIDC sub
                            #   repo:<org>@*/*:*    — immutable-ID subject customization,
                            #                         where the sub carries repo:<org>@<orgId>/...
                            # The `@*` sits BETWEEN the org and the `/`, so it only matches
                            # THIS org's immutable-id form — it can't prefix-match a sibling
                            # login (e.g. `<org>-evil`), keeping the trust boundary tight. A
                            # bare `repo:<org>*` would widen trust to sibling orgs, so avoid it.
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

    # -- lifecycle ----------------------------------------------------------

    def ensure_role(self, org: str) -> Optional[str]:
        """Create-or-update the per-org role; return its ARN (None if the service is inert).

        Idempotent: an existing role has its trust + inline policy refreshed (so a trust
        drift or a re-connect converges). Safe to call on every connect."""
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
                    Description=f"Per-org GitHub OIDC ECR-push role (E22) for org {org}",
                    Tags=[
                        {"Key": "ManagedBy", "Value": "agp"},
                        {"Key": "Purpose", "Value": "github-oidc-ecr-push"},
                    ],
                )
            except ClientError as err:
                if err.response.get("Error", {}).get("Code") == "EntityAlreadyExists":
                    # Converge an existing role: refresh trust (org can't change, but drift
                    # or a policy-version bump is healed here).
                    self._iam.update_assume_role_policy(
                        RoleName=name, PolicyDocument=trust
                    )
                else:
                    raise
            # (Re)attach the inline permission policy — PutRolePolicy is idempotent.
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
        """Ensure the shared platform-default push role exists; return its ARN (None if inert).

        ADOPT-DON'T-RETRUST. An existing role's trust policy is left EXACTLY as found and
        only its inline permission policy is refreshed. Two reasons, and both matter:

          1. On a deployment that already applied the old Terraform, the live role IS the
             Terraform one — the point of matching its name is to adopt it, and rewriting
             the trust of a role GitHub Actions is actively using is a live change nobody
             asked for.
          2. This role is SHARED across every connected org, but a trust policy names ONE
             org (Terraform's ``var.github_org``). Refreshing it per connection would make
             the last connect win and silently revoke the previous org's fallback.

        WHAT MAKES (2) SAFE, STATED AS A MECHANISM RATHER THAN A CLAIM. Multi-org works
        because the PER-ORG role WINS AT STAMPING: ``ProjectService.set_repo_vars`` writes
        ``connection.ecr_push_role_arn`` into the repo's ``AWS_ECR_PUSH_ROLE_ARN`` var when
        the repo's connection has one, in preference to the tenant stage's value — in BOTH
        the tenant-wired and the legacy branch. So org B's repos receive
        ``<prefix>-ecr-push-B`` (created by ``ensure_role``, trusting ``repo:B/*:*``) and
        never the shared role, and the shared role's single-org trust is reached only by a
        connection with NO per-org role: the single-org / legacy / inert-service fallback.

        That precedence is LOAD-BEARING for this method's correctness, not incidental. It
        used to live only in the ``self._tenants is None`` arm, which production never takes
        — so every org but the first was handed the shared role, whose trust named the
        first, and every one of their builds failed at "Configure AWS credentials".
        ``test_the_per_org_push_role_WINS_over_the_tenant_stage_value`` is the fence around
        it; if that test is ever deleted, this docstring's premise dies with it.

        The inline policy IS refreshed because its content is org-independent (the shared
        agent-images repo ARN), so convergence there is free of that hazard.

        ``org`` therefore only shapes the trust of a role that does NOT exist yet — the
        first connection on a fresh account, which is exactly the case Terraform can no
        longer serve (IAM validates the ``Federated`` principal at create time, and on a
        fresh account the OIDC provider does not exist during apply)."""
        if not self.enabled:
            logger.info("[ecr-push-role] service inert (unconfigured); skipping shared ensure")
            return None

        name = self.shared_role_name()
        # Built BEFORE the try, like ``ensure_role`` does: the org validation inside
        # ``_trust_policy`` must reject a wildcard org as a caller error (``bad_request``)
        # rather than get caught by the ``ClientError`` handler below and relabelled an
        # IAM failure. Nothing is created when it raises.
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
                # Already there (Terraform's, or an earlier connection's) — adopt it.
            self._iam.put_role_policy(
                RoleName=name,
                PolicyName=_INLINE_POLICY_NAME,
                PolicyDocument=self._permission_policy(),
            )
        except ClientError as err:
            logger.exception("[ecr-push-role] ensure_shared_role failed for %s", name)
            raise EcrPushRoleError(
                "Failed to ensure the shared ECR-push role", kind="iam_error"
            ) from err

        arn = self._iam.get_role(RoleName=name)["Role"]["Arn"]
        logger.info("[ecr-push-role] ensured shared role %s", name)
        return arn

    def delete_role(self, org: str) -> None:
        """Delete the per-org role + its inline policy. Idempotent — a missing role/policy
        is a no-op (so a double-disconnect or a never-provisioned org is safe). Inert when
        unconfigured."""
        if not self.enabled:
            return

        name = self.role_name(org)
        # Delete the inline policy first (a role with attached policies can't be deleted).
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
