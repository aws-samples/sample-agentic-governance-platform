"""The cross-account credential seam for tenant-account resources.

A tenant stage that carries a ``deploy_role_arn`` deploys its runtime into the tenant's account,
so every call that addresses such a resource must run under that role: the control plane's
ambient credentials would answer "not found" from the wrong account, which reads as the
idempotent already-done state. :func:`stage_client` is the one place that decides which account
a call lands in. Each call assumes afresh (no credential cache), and a failed assume raises
:class:`TenantCredentialsError` rather than falling back to the ambient client; the caller
reports it. :func:`probe_stage_reach` is the read-only counterpart that reports whether the hop
is open and never raises.

No message or log line carries an account id: a ``deploy_role_arn`` contains one, so errors
carry the role name, parsed by :func:`_safe_role_label` and never sliced, plus the exception's
type name, and nothing from a provider body. ``_ROLE_ARN_RE`` is the parse-side twin of the write
rule in ``tenant_service`` and accepts the same set, so every stored ARN yields a role name here.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import ClassVar, Optional

from botocore.exceptions import BotoCoreError, ClientError

from core import aws_clients
from core.aws_clients import AssumedCredentials
from core.errors import AppError, Outcome
from models.tenant import TenantStageConfig

logger = logging.getLogger(__name__)

# Decision: docs/adr/backend-tenant-account-credentials.md

# STS caps RoleSessionName at 64 characters: https://docs.aws.amazon.com/STS/latest/APIReference/API_AssumeRole.html
_MAX_SESSION_NAME = 64
_SESSION_PREFIX = "agp-"
_ROLE_ARN_RE = re.compile(
    r"^arn:aws[\w-]*:iam::[0-9]{12}:role/(?:[\w+=,.@-]{1,64}/)*(?P<name>[\w+=,.@-]{1,64})\Z"
)
_UNPARSEABLE_ROLE_LABEL = "deploy role (malformed ARN)"


def _safe_role_label(deploy_role_arn: str) -> str:
    """The role name for messages and logs, or a generic label when the ARN does not parse. The
    assume is still attempted with the raw ARN; STS is the authority on what it accepts."""
    match = _ROLE_ARN_RE.match(deploy_role_arn or "")
    return match.group("name") if match else _UNPARSEABLE_ROLE_LABEL


class TenantCredentialsError(AppError):
    """AssumeRole into a tenant account failed. The message is the role name and the exception's
    type name, never an account id, an ARN or a provider body; the one kind has a fixed detail."""

    domain: ClassVar[str] = "tenant"
    kinds: ClassVar[Mapping[str, Outcome]] = {
        "assume_role_failed": Outcome(502, "could not assume the tenant deploy role"),
    }

    def __init__(self, message: str, kind: str = "assume_role_failed") -> None:
        super().__init__(message, kind)


class StageUnresolvedError(AppError):
    """The account that owns a stage's resources could not be determined: the record names a
    stage, and the lookup that would have told us its account failed or came back without it.
    Distinct from :class:`TenantCredentialsError`, which means the account is known and could not
    be entered. Leaf services that discover the same condition subclass this, so one ``except``
    arm renders both. The message names a stage or a lookup state, never a record, an ARN or an
    account id; the one kind has a fixed detail."""

    domain: ClassVar[str] = "tenant"
    kinds: ClassVar[Mapping[str, Outcome]] = {
        "stage_unresolved": Outcome(502, "could not resolve the account that owns the stage"),
    }

    def __init__(self, message: str) -> None:
        super().__init__(message, "stage_unresolved")


def stage_client(service_name: str, cfg: Optional[TenantStageConfig], *, session_suffix: str):
    """A boto3 client for the account owning ``cfg``.

    ``cfg is None`` (no stage could be resolved) is a bare ambient client; ``cfg`` with an empty
    ``deploy_role_arn`` (deploy-in-place) is an ambient client in the stage's region. Neither
    touches STS. Otherwise ``sts:AssumeRole`` on ``cfg.deploy_role_arn`` with the session name
    ``agp-<session_suffix>`` and a client from the temporary credentials; a failed assume raises
    :class:`TenantCredentialsError`.
    """
    if cfg is None:
        return aws_clients.client(service_name)
    if not cfg.deploy_role_arn:
        return aws_clients.client(service_name, region=cfg.region)

    session_name = f"{_SESSION_PREFIX}{session_suffix}"[:_MAX_SESSION_NAME]
    role_name = _safe_role_label(cfg.deploy_role_arn)
    try:
        sts = aws_clients.client("sts", region=cfg.region)
        granted = sts.assume_role(RoleArn=cfg.deploy_role_arn, RoleSessionName=session_name)["Credentials"]
        creds = AssumedCredentials(
            access_key_id=granted["AccessKeyId"],
            secret_access_key=granted["SecretAccessKey"],
            session_token=granted["SessionToken"],
        )
    except (ClientError, BotoCoreError, KeyError, TypeError) as err:
        logger.warning(
            "[tenant-credentials] assume-role failed for %s as %s (%s)",
            role_name,
            service_name,
            type(err).__name__,
        )
        raise TenantCredentialsError(f"{role_name} ({type(err).__name__})") from err

    return aws_clients.client(service_name, region=cfg.region, credentials=creds)


def probe_stage_reach(role_arn: str, region: str) -> tuple[bool, str]:
    """Can this backend assume ``role_arn``? Returns ``(True, "")`` when the assume succeeded and
    ``(False, <reason>)`` when it was refused, where the reason is a botocore error code or an
    exception class name for the log only. Never raises. Both a missing backend-side grant and a
    tenant-side trust policy that does not yet admit the backend render as ``False``."""
    session_name = f"{_SESSION_PREFIX}probe"[:_MAX_SESSION_NAME]
    role_name = _safe_role_label(role_arn)
    try:
        sts = aws_clients.client("sts", region=region or "us-east-1")
        sts.assume_role(RoleArn=role_arn, RoleSessionName=session_name)
    except ClientError as err:
        code = err.response.get("Error", {}).get("Code", "ClientError")
        logger.info("[tenant-credentials] reach probe refused for %s (%s)", role_name, code)
        return False, code
    except (BotoCoreError, KeyError, TypeError) as err:
        logger.info(
            "[tenant-credentials] reach probe failed for %s (%s)",
            role_name,
            type(err).__name__,
        )
        return False, type(err).__name__
    return True, ""
