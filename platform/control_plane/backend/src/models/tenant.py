"""Pydantic models for Tenants, the multi-tenancy unit of ownership.

A ``Tenant`` is a line-of-business unit that owns agents, MCP servers and projects (they carry a
``tenant_id``), maps to one or more Entra groups and carries a per-stage platform config. A tenant is
platform-typed: ``platform`` is an immutable ``TenantPlatform`` discriminator and ``stages`` a union of
per-platform shapes, ``TenantStageConfig`` (AWS) and ``DatabricksStageConfig``. Records written before
platform typing hydrate as ``aws`` through ``hydrate_tenant_item``, which also folds the older flat
``aws_account_*`` fields into ``stages``. ``TenantUpdate`` carries no ``platform``: a live tenant is never
re-typed, and the service refuses a stages update whose shape contradicts the stored platform.

Credentials are write-only: ``DatabricksStageConfig.sp_client_secret`` and the ``account_admin_*`` pair
on ``TenantCreate`` / ``TenantUpdate`` are accepted on the way in and absent on the way out; the read
model carries only the two Secrets Manager pointers. The JSON keys inside those secrets are declared
here, next to the ARN fields that point at them, because the writer (``tenant_service``) and the
readers (``runtime_catalog``, ``databricks_identity_service``) are different modules and a key spelled
twice drifts into a silent "no credential found".

``binding_mode`` is the closed ``TenantBindingMode``; a stored value outside it folds to ``""`` with
a warning naming the tenant, and the document promises the set.

The regexes are security boundaries matched with ``fullmatch`` at every call site (``$`` alone also
matches before a trailing newline) and pinned over hostile inputs in ``tests/test_tenant_model.py``.
Persistence and validation orchestration live in ``tenant_service``; no boto3, no I/O here.
"""

from __future__ import annotations

import logging
import re
from enum import Enum
from typing import Dict, List, Literal, Optional, Union, get_args

from pydantic import Field, PrivateAttr, ValidationInfo, field_validator

from models.base import WireModel

logger = logging.getLogger(__name__)

ACCOUNT_ID_RE = re.compile(r"^\d{12}$")

WORKSPACE_URL_RE = re.compile(r"^https://[a-z0-9][a-z0-9.-]+[a-z0-9]$", re.ASCII)

WORKSPACE_ID_RE = re.compile(r"^\d+$", re.ASCII)

DATABRICKS_ACCOUNT_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.ASCII
)

SP_SECRET_KEY = "sp_client_secret"  # noqa: S105 — a key name, not a credential; expires 2027-10-23; added E49A-50A/T3
ACCOUNT_ADMIN_ID_KEY = "account_admin_client_id"
ACCOUNT_ADMIN_SECRET_KEY = "account_admin_secret"  # noqa: S105 — a key name, not a credential; expires 2027-10-23; added E49A-50A/T3


class TenantPlatform(str, Enum):
    """The runtime platform a tenant's agents live on; immutable after create. A ``str`` enum so it
    serializes to and compares equal to the wire value."""

    AWS = "aws"
    DATABRICKS = "databricks"


class TenantStageConfig(WireModel):
    """Per-stage AWS deployment config. The ECR repository and the role ARNs default empty and are
    populated as the cross-account CICD provisions them. ``account_id`` is validated on write against
    ``ACCOUNT_ID_RE``."""

    account_id: str = Field(description="A 12-digit AWS account id, validated on write")
    region: str = "us-east-1"
    ecr_repo_uri: str = ""
    push_role_arn: str = ""
    deploy_role_arn: str = ""


class DatabricksStageConfig(WireModel):
    """Per-stage Databricks workspace config. The stage's identity is the workspace service principal:
    ``sp_client_id`` on the record, its secret only in Secrets Manager.

    ``sp_client_secret`` is write-only, one field with two directions: on the way in it carries what
    the admin typed (before it existed, pydantic's ``extra="ignore"`` swallowed the key the form sent
    and the API answered 201 with nothing stored); on the way out ``exclude=True`` removes it from
    every ``model_dump`` and ``model_dump_json``, which are the FastAPI response and the DynamoDB
    persist alike. The same ``DatabricksStageConfig`` is what a create body carries, what the record
    stores and what the ``StageConfig`` union resolves on read, so one model with an excluded field
    replaces the two-model split ``connection.py`` uses.

    ``workspace_url`` and ``account_id`` are validated on write against ``WORKSPACE_URL_RE`` and
    ``DATABRICKS_ACCOUNT_ID_RE``; the account id is interpolated into the account-admin token-mint
    path, the highest-privilege call, so a traversal-shaped value is refused before it is stored
    even though the client also quotes it.
    """

    workspace_url: str = Field(
        description="An https origin with no port, path or trailing slash, validated on write"
    )
    workspace_id: str = Field(
        default="0",
        description=(
            'A digits-string, not an int: an opaque identifier where "0" is a real value (a workspace URL '
            "with no o= parameter)"
        ),
    )
    cloud: str = Field(default="aws", description="aws, azure or gcp")
    region: str = ""
    account_id: str = Field(
        default="",
        description=(
            "The Databricks account UUID, required for federation binding mode only and validated on write"
        ),
    )
    sp_client_id: str = ""
    sp_client_secret: str = Field(
        default="",
        exclude=True,
        description="Write-only; the service consumes it into Secrets Manager and persists only the ARN",
    )
    sp_client_secret_arn: str = Field(
        default="", description="Secrets Manager ARN; the secret is never in DynamoDB"
    )


StageConfig = Union[TenantStageConfig, DatabricksStageConfig]

TenantBindingMode = Literal["", "federation", "invoke_unavailable", "sp_secret"]


class Tenant(WireModel):
    """Read model: a line-of-business tenant. Carries only metadata; a Databricks tenant's secrets live
    in Secrets Manager and only their ARNs are here. ``stages`` resolves each stored dict to the model
    its keys fit (pydantic's smart union); the service refuses a shape that contradicts the stored
    platform. ``binding_mode`` is the closed ``TenantBindingMode``: ``services.tenant_service``, the
    only writer, writes members, and a stored value outside the set folds to ``""`` with a warning."""

    id: str = Field(description='"ten-<8 hex>", or "default" for the seed tenant')
    name: str
    line_of_business: str
    entra_group_ids: List[str] = Field(description="At least one group")
    platform: TenantPlatform = Field(default=TenantPlatform.AWS, description="Immutable after create")
    stages: Dict[str, StageConfig] = Field(description="Per-stage platform config, e.g. dev and prod")
    capabilities: Dict[str, bool] = Field(
        default_factory=dict,
        description=(
            "Written by the connect-time probe, read-only to clients: can_discover, account_admin, "
            "user_sync; absent means never probed"
        ),
    )
    binding_mode: TenantBindingMode = Field(
        default="",
        description=(
            '"" (aws), federation, invoke_unavailable or sp_secret (dormant); written by the connect-time '
            "probe, read-only to clients"
        ),
    )
    account_admin_secret_arn: str = Field(
        default="",
        description=(
            "Secrets Manager ARN of the optional account-admin credential; the ARN only, since the client "
            "id and the secret both live inside the secret's JSON body. Empty is the ordinary case: "
            "federation is an extra grant a customer may not have made"
        ),
    )
    description: str = ""
    created_by: str
    created_at: str
    updated_at: str
    _row_version: int | None = PrivateAttr(default=None)

    @field_validator("binding_mode", mode="before")
    @classmethod
    def _fold_binding_mode(cls, value: object, info: ValidationInfo) -> object:
        if value in get_args(TenantBindingMode):
            return value
        logger.warning(
            "%s %r is outside its set on %s; folded to %r", "binding_mode", value, info.data.get("id"), ""
        )
        return ""


class TenantCreate(WireModel):
    """Write-only input: the ``POST /tenants`` body. ``platform`` is settable here and only here; the
    capability fields are not client-writable, the connect-time probe owns them.

    The optional account-admin credential is two plain strings rather than a nested object because the
    pair only means anything together (an id with no secret cannot mint a token), and the frontend
    already refuses a half-filled pair. The service puts both halves in Secrets Manager and persists
    only ``Tenant.account_admin_secret_arn``. They unlock ``federation`` binding mode; without them the
    account-level federation probe cannot run and the tenant is badged ``invoke_unavailable``.
    """

    name: str
    line_of_business: str
    entra_group_ids: List[str]
    platform: TenantPlatform = TenantPlatform.AWS
    stages: Dict[str, StageConfig]
    account_admin_client_id: str = Field(default="", description="Write-only; Databricks only")
    account_admin_secret: str = Field(default="", description="Write-only; Databricks only")
    description: str = ""


class TenantUpdate(WireModel):
    """Write-only input: the ``PUT /tenants/{id}`` body, every field optional.

    No ``platform`` field, by contract: a body carrying one is dropped rather than obeyed. A ``stages``
    update must still match the stored platform's shape, enforced in ``tenant_service``. The
    account-admin pair rotates the write-only credential so an admin can unlock federation on a tenant
    that connected without it: ``None`` (unset) leaves the stored credential alone and a supplied pair
    replaces it, which the service reads off ``exclude_unset``, hence ``Optional`` rather than the
    create model's ``""``. ``account_admin_secret_arn`` is deliberately absent: the ARN is the server's
    value, and a client that could send one could re-point a tenant at another tenant's secret; the
    per-stage ``sp_client_secret_arn`` lives inside the ``stages`` shape, so the service ignores a
    body-supplied one explicitly.
    """

    name: Optional[str] = None
    line_of_business: Optional[str] = None
    entra_group_ids: Optional[List[str]] = None
    stages: Optional[Dict[str, StageConfig]] = None
    account_admin_client_id: Optional[str] = Field(
        default=None, description="Write-only; rotates the credential"
    )
    account_admin_secret: Optional[str] = Field(
        default=None, description="Write-only; rotates the credential"
    )
    description: Optional[str] = None


class Verb(str, Enum):
    """The capabilities a tenant surface reports on, one per named requirement. ``MIRROR`` is the
    platform-side ACL write; ``MCP_DELIVERY`` is serving MCP servers to a runtime; ``OBSERVABILITY``
    is having a trace destination at all (whether AGP can wire it is
    ``PlatformDeclaration.trace_auto_delivery``, a separate fact)."""

    DISCOVER = "discover"
    INVOKE = "invoke"
    MIRROR = "mirror"
    DEPLOY = "deploy"
    PUBLISH = "publish"
    GUARDRAILS = "guardrails"
    MCP_DELIVERY = "mcp_delivery"
    OBSERVABILITY = "observability"


ReadinessReason = Literal[
    "",
    "unsupported",
    "deploy_role_not_assumable",
    "probe_unreachable",
    "invoke_unavailable",
]


class VerbReadiness(WireModel):
    """One verb's verdict for one tenant, derived by ``services.platform_registry.tenant_readiness``
    and never stored. ``verb`` is the entry's own key in the readiness map, carried so the document
    types the key set. ``reason`` is one of ``platform_registry.READINESS_REASONS``, a closed set the
    frontend maps to copy exhaustively, and is ``""`` unless the state is ``unavailable``. ``stage``
    is the only variable part and travels in its own field, ``None`` unless the verdict is
    stage-scoped, never as a suffix on ``reason``: it is a key from the tenant's own stage map, and
    keeping it out of ``reason`` is what keeps the reason set closed."""

    verb: Verb
    state: Literal["ready", "unavailable", "not_applicable"]
    reason: ReadinessReason
    stage: Optional[str] = None


class TenantView(Tenant):
    """The tenant as the admin list and the probe answer it: every ``Tenant`` field plus
    ``readiness``, derived per verb from the stored ``capabilities`` and ``binding_mode`` on the way
    out, so the two cannot disagree, and never stored."""

    readiness: Dict[str, VerbReadiness] = Field(
        description="Keyed by the verb's wire value, one entry per Verb; each entry repeats its verb"
    )


def tenant_platform(tenant: Tenant) -> str:
    """The tenant's platform as the one string the template picker filters on
    (``infra_template.platform == tenant.platform``).

    ``"aws"`` is the fold for a blank or ``None`` value, because a record written before the field
    existed hydrates with no value and every such tenant is an AWS tenant; the fold matters because a
    ``""`` filter key would match no infra template and empty the picker, which reads as "no templates"
    rather than "unknown platform". Takes a ``Tenant``, not any attribute-bearing object, so a raw item
    cannot be mistaken for an AWS tenant. Returns the enum's ``.value``, never ``str()``, which renders
    ``TenantPlatform.AWS`` and matches no template.
    """
    platform = getattr(tenant, "platform", None) or TenantPlatform.AWS
    return getattr(platform, "value", platform)


def hydrate_tenant_item(clean: dict) -> dict:
    """Fold legacy tenant items forward on read, mutating and returning the same dict.

    A record written before platform typing has no ``platform`` key and defaults to ``"aws"``; an
    explicit platform is never overwritten, and ``capabilities`` / ``binding_mode`` need no defaulting
    because the model's own defaults cover them. A record written before nested stages has flat
    ``aws_account_dev`` / ``aws_account_prod`` / ``aws_region`` fields: they become a ``stages`` map
    (dev and prod, both inheriting the flat region, ECR and roles empty) and the flat keys are dropped;
    a ``stages`` key already present is left alone.
    """
    clean.setdefault("platform", TenantPlatform.AWS.value)
    if "stages" in clean or "aws_account_dev" not in clean:
        return clean
    region = clean.get("aws_region", "us-east-1")
    clean["stages"] = {
        "dev": {"account_id": clean["aws_account_dev"], "region": region},
        "prod": {"account_id": clean["aws_account_prod"], "region": region},
    }
    for key in ("aws_account_dev", "aws_account_prod", "aws_region"):
        clean.pop(key, None)
    return clean
