"""The cross-account credential seam for tenant-account resources (E36/T8, item 1).

THE DEFECT THIS EXISTS FOR. Every AWS client in the service layer was built from the
backend's AMBIENT ECS-task credentials, which live in the CONTROL-PLANE account. A tenant
stage that carries a ``deploy_role_arn`` deploys its runtime into the TENANT's account
(``modules/agentcore_runtime/versions.tf`` assumes that role; the ARN comes from
``TenantStageConfig.deploy_role_arn`` via ``runtime_build_service``). So the teardown paths
never addressed the resource at all: ``get_agent_runtime`` answered
``ResourceNotFoundException`` *in the control-plane account*, ``delete_role`` answered
``NoSuchEntity`` there, and both of those are the IDEMPOTENT-already-done state — the
cascade reported ``deleted`` on a runtime that kept billing and an account-global role that
kept blocking re-materialization. Not a swallowed error: a truthful answer to the wrong
question.

:func:`stage_client` is the ONE place that decides which account a call lands in.

WHY THERE IS NO CREDENTIAL CACHE. Each call assumes afresh. A cached session has a floor as
low as 15 minutes on some roles and nothing here would refresh it, so a cache would trade a
sub-second STS call for a class of "worked in the morning" failures. Teardown is a handful of
calls per repo, not a hot path.

WHY A FAILED ASSUME MUST RAISE. Falling back to the ambient client on failure would
reproduce the original defect exactly — the call would land in the control-plane account,
answer NotFound, and be reported as a successful teardown. So the failure is loud
(:class:`TenantCredentialsError`) and the caller is responsible for reporting it honestly.
See ``ProjectService._delete_exec_role`` / ``_delete_runtime`` for the report shape.

NO ACCOUNT ID IN ANY MESSAGE — the hard project rule, logs included. A ``deploy_role_arn``
CONTAINS the 12-digit account id, so the error carries the role NAME (the actionable fact:
what an operator types into the IAM console) plus ``type(err).__name__`` and NOTHING from the
provider's body, which can hold a request id, an ARN, or a credential. Same rule as
``ProjectService._reclaim_exec_role``'s hints and the RID-not-ARN logging idiom. The role name
is PARSED (:func:`_safe_role_label`), never sliced — see that function for the leak that
slicing caused.

WHAT THIS DOES NOT DO. It does not grant anything — but the grant now EXISTS. E36/T11 landed
both halves: the backend ECS task role holds ``sts:AssumeRole`` on
``arn:aws:iam::*:role/agp-deployment-*`` (``modules/ecs/main.tf``), and the platform's own
deploy role trusts TWO principals — the CodeBuild role that provisions and this task role
that tears down (``modules/default_tenant/main.tf``). That grant reaches nothing on its own:
any account can hold a role named ``agp-deployment-*``, so THE TENANT-SIDE TRUST POLICY IS
THE REAL GATE. A tenant account's deploy role is hand-built, and until its owner adds the
task role as a principal (docs/tenant-account-onboarding.md) the assume still FAILS — the
point being that it now fails VISIBLY (``assume_role_failed:``) instead of reporting success.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from models.tenant import TenantStageConfig

logger = logging.getLogger(__name__)

# STS caps ``RoleSessionName`` at 64 characters and answers a hard ValidationError beyond it
# — a teardown that fails on the NAME rather than on a permission, which is the least
# useful failure available. Truncation is therefore not cosmetic.
_MAX_SESSION_NAME = 64

# Every session this platform opens in a tenant account is identifiable as ours.
_SESSION_PREFIX = "agp-"

# An IAM role ARN, with the role NAME captured and any IAM path discarded. ``models/tenant.py``
# still declares ``deploy_role_arn: str = ""`` with no pattern, and records written before
# E36/T11 (or seeded via ``TenantService.upsert_seed``, which bypasses the write rule) can hold
# anything — so its shape stays something to PARSE, never to assume, even though
# ``TenantService._validate_stages`` now enforces this same shape on create/update.
#
# THIS PATTERN AND ``tenant_service._ROLE_ARN_RE`` MUST STAY BYTE-IDENTICAL apart from the
# ``name`` group: the write rule and this parse are ONE contract (see that module's comment for
# why the path is charset-bounded, why ``[0-9]{12}`` not ``\d{12}``, and why ``\Z`` not ``$``).
_ROLE_ARN_RE = re.compile(
    r"^arn:aws[\w-]*:iam::[0-9]{12}:role/(?:[\w+=,.@-]{1,64}/)*(?P<name>[\w+=,.@-]{1,64})\Z"
)

# What an ARN we could not parse is called downstream. GENERIC ON PURPOSE: the only thing left
# to name it after would be the raw value, which is the one string that may carry the account id.
_UNPARSEABLE_ROLE_LABEL = "deploy role (malformed ARN)"


def _safe_role_label(deploy_role_arn: str) -> str:
    """The role NAME for messages/logs, or a generic label when the ARN does not parse.

    THE LEAK THIS REPLACES. This was ``deploy_role_arn.rsplit("/", 1)[-1]``, and ``rsplit`` on
    a string with no ``/`` returns the WHOLE string. Since nothing validates the field, an
    admin typo (``:role-agp-deployment`` instead of ``:role/…``), a bare account id, or
    ``…:root`` all put the tenant's 12-digit account id into
    :class:`TenantCredentialsError`'s message — which travels into the cascade's ``reason``
    (rendered by the delete modal), into ``RepoDeleteResult``, and into CloudWatch via the
    ``logger.warning`` below. Every existing safety test used a well-formed ARN, so none of
    them could see it.

    Parsing fails CLOSED into a label that names nothing: an unusable-but-safe string beats a
    precise-but-leaking one, and the value is only ever read on a failure path where the
    operator's action is "look at the tenant's stage config" either way. The assume is still
    ATTEMPTED with the raw ARN — STS is the authority on what it accepts, and refusing here on
    a regex we wrote would turn a working teardown into a failure over a shape we failed to
    anticipate."""
    match = _ROLE_ARN_RE.match(deploy_role_arn or "")
    return match.group("name") if match else _UNPARSEABLE_ROLE_LABEL


class TenantCredentialsError(Exception):
    """AssumeRole into a tenant account failed. Message is SAFE (role name + exception
    type, never an account id, an ARN, or a provider body).

    ``kind`` mirrors the ``ConnectionError``/``GitHubOidcProviderError`` idiom so a caller
    can branch without string-matching; there is exactly one kind today because there is
    exactly one failure mode — we did not get into that account.
    """

    def __init__(self, message: str, kind: str = "assume_role_failed") -> None:
        super().__init__(message)
        self.message = message
        self.kind = kind


class StageUnresolvedError(Exception):
    """The account that owns this stage's resources could not be DETERMINED (E36/T8 fix 1).

    Distinct from :class:`TenantCredentialsError`, which means we know the account and could
    not get INTO it. This one means we do not know which account to ask, and it exists because
    "we could not look it up" was being silently downgraded to "use the control-plane client" —
    where a tenant's runtime is always ``ResourceNotFoundException`` and its role always
    ``NoSuchEntity``, i.e. the idempotent already-done state. That is the exact defect T8
    closes, re-manufactured out of a DynamoDB throttle.

    Ambient is still correct for the genuinely-unknown shapes (no tenant id, no tenant service,
    no stage, :data:`models.agent.UNKNOWN_STAGE`) — see ``ProjectService._stage_cfg``. This is
    raised only when the record DOES name a stage and the lookup that would have told us its
    account failed or came back without it.

    It lives HERE, next to the credential seam, rather than in ``project_service`` (which
    re-exports it for its own callers): every teardown report prefix in the platform is
    ``assume_role_failed:`` or ``stage_unresolved:``, and a LEAF service that discovers the
    same condition must be able to subclass this without importing 3.8k lines of
    ``project_service`` — which is what
    ``langfuse_provisioning.LangfuseAccountUnresolvedError`` does (E36/T16 fix round 1), so
    one ``except`` arm renders both.

    Message is SAFE by construction: a stage NAME or a lookup state, never a tenant record,
    an ARN or an account id."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def stage_client(service_name: str, cfg: Optional[TenantStageConfig], *, session_suffix: str):
    """boto3 client for the account owning ``cfg``.

    ``cfg`` None or ``cfg.deploy_role_arn`` falsy -> ambient client (today's behavior).
    Else ``sts:AssumeRole(cfg.deploy_role_arn, RoleSessionName=f"agp-{session_suffix}"[:64])``
    -> client from temp creds. Raises :class:`TenantCredentialsError` on AssumeRole failure.

    The two ambient cases are NOT the same shape, and the difference is deliberate:

    * ``cfg is None`` — the caller could not resolve a stage at all (no tenant service, a
      legacy record whose stage is :data:`models.agent.UNKNOWN_STAGE`, a stage the tenant no
      longer lists). Nothing is known, so nothing is asserted: a bare
      ``boto3.client(service_name)`` resolving its region the way every other ambient client
      in the backend does.
    * ``cfg`` present with an empty ``deploy_role_arn`` — deploy-in-place. Ambient
      CREDENTIALS (which is what "ambient" means, and is what keeps a single-account tenant
      byte-for-byte on today's behaviour), but the STAGE's region, because we know it.

    Neither ambient case touches STS. An assume the caller did not ask for would need a grant
    the ECS task role does not hold, so it would turn every single-account teardown into a
    failure.
    """
    if cfg is None:
        return boto3.client(service_name)
    if not cfg.deploy_role_arn:
        return boto3.client(service_name, region_name=cfg.region)

    session_name = f"{_SESSION_PREFIX}{session_suffix}"[:_MAX_SESSION_NAME]
    # Role NAME only, PARSED — the ARN carries the account id, and slicing on a `/` that
    # nothing enforces leaked the whole ARN. See :func:`_safe_role_label`.
    role_name = _safe_role_label(cfg.deploy_role_arn)
    try:
        sts = boto3.client("sts", region_name=cfg.region)
        creds = sts.assume_role(
            RoleArn=cfg.deploy_role_arn, RoleSessionName=session_name
        )["Credentials"]
        key_id = creds["AccessKeyId"]
        secret = creds["SecretAccessKey"]
        token = creds["SessionToken"]
    except (ClientError, BotoCoreError, KeyError, TypeError) as err:
        # KeyError/TypeError: a malformed response must not escape as something a caller's
        # ``except TenantCredentialsError`` would miss and report as a generic step failure.
        logger.warning(
            "[tenant-credentials] assume-role failed for %s as %s (%s)",
            role_name,
            service_name,
            type(err).__name__,
        )
        raise TenantCredentialsError(f"{role_name} ({type(err).__name__})") from err

    return boto3.client(
        service_name,
        region_name=cfg.region,
        aws_access_key_id=key_id,
        aws_secret_access_key=secret,
        aws_session_token=token,
    )
