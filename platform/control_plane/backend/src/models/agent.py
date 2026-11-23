"""Agent Registry domain models and the lifecycle-to-status mapping.

An agent is one Custom record in AWS Agent Registry (Amazon Bedrock AgentCore). The native record
carries ``name``, ``description``, ``status``, ``createdAt`` and ``updatedAt``; every governance field
is serialized into the record's ``descriptors.custom.data`` JSON, the envelope, by ``Agent.to_envelope``
and read back by ``Agent.from_record``. ``lifecycle_state`` is derived from the native ``status`` and
never stored.

Service-written fields (identity ids, Langfuse join fields, ``project_id``, ``marketplace``,
``bound_stage``) live on ``Agent`` and the envelope only, never on ``AgentCreate`` or ``AgentUpdate``,
so a request body cannot forge an attestation or re-parent a record; pydantic's default
``extra="ignore"`` drops such a key. Every envelope key is written on every save, as ``None`` or an
empty container when unset, so a later persist cannot drop a value an external writer (the CodeBuild
buildspec) patched in; a key missing from a stored envelope hydrates to the field's default, which
keeps ``ENVELOPE_SCHEMA_VERSION`` at 1 through every additive round.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from enum import Enum
from typing import Dict, List, Literal, Optional, get_args

from pydantic import ConfigDict, Field, field_validator, model_validator

from models.base import WireModel
from models.marketplace import MarketplacePublication
from models.tenant import TenantBindingMode, TenantPlatform

logger = logging.getLogger(__name__)


class LifecycleState(str, Enum):
    PROPOSED = "proposed"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    DEPRECATED = "deprecated"


class Platform(str, Enum):
    AWS_BEDROCK = "aws_bedrock"
    AZURE = "azure"
    SALESFORCE = "salesforce"
    SAP = "sap"
    DATABRICKS = "databricks"
    GOOGLE = "google"
    ON_PREM = "on_prem"
    OTHER = "other"


GOVERNABLE_PLATFORMS: Dict[Platform, TenantPlatform] = {
    Platform.AWS_BEDROCK: TenantPlatform.AWS,
    Platform.DATABRICKS: TenantPlatform.DATABRICKS,
}

INVENTORY_ONLY_PLATFORMS: frozenset = frozenset(frozenset(Platform) - GOVERNABLE_PLATFORMS.keys())


def is_inventory_only(platform: "Platform | str") -> bool:
    """True when a record's platform label is registry-only: AGP has no adapter for it and cannot
    govern or invoke it.

    ``Platform`` is the label an operator picks at registration; ``TenantPlatform`` is the set of
    platforms AGP acts on. ``GOVERNABLE_PLATFORMS`` is the one translation between the two and lives
    beside ``Platform`` because the module that declares a vocabulary owns the translation out of it;
    ``INVENTORY_ONLY_PLATFORMS`` is derived from it, so a new ``Platform`` member is inventory-only
    until an adapter claims it. Accepts the enum or its wire string. An unknown label raises
    ``ValueError`` from the enum lookup rather than answering True: "never heard of this platform"
    and "inventory-only" are different facts.
    """
    return Platform(platform) in INVENTORY_ONLY_PLATFORMS


class DataClassification(str, Enum):
    PUBLIC = "Public"
    INTERNAL = "Internal"
    CONFIDENTIAL = "Confidential"
    RESTRICTED = "Restricted"


class Origin(str, Enum):
    DEPLOYED = "Deployed"
    REGISTERED = "Registered"


class AgentAuthType(str, Enum):
    """How a caller authenticates to invoke the agent."""

    NONE = "none"
    ENTRA = "entra"
    API_KEY = "api_key"


class IdentityStatus(str, Enum):
    """Where a record stands in Entra identity provisioning; ``provisioned`` is the gate invoke and
    every grant route check.

    A ``str`` enum because the gates compare it to a bare string. Provisioning writers assign a
    member: pydantic does not validate assignment, so a bare string would bypass this vocabulary and
    make the serializer warn on every response carrying the record.
    """

    NONE = "none"
    PENDING = "pending"
    PROVISIONED = "provisioned"
    FAILED = "failed"


def coerce_identity_status(raw) -> "IdentityStatus":
    """Total coercion of a stored ``identity_status`` into ``IdentityStatus``.

    Unlike ``lifecycle_for_status``, which raises on an unknown value, this one tolerates a foreign
    value because the field has an external writer: the CodeBuild buildspec patches
    ``identity_status="provisioned"`` into the stored envelope with ``jq``
    (``platform/control_plane/infrastructure/modules/codebuild/buildspec.yml``), and raising here
    would let one bad record 500 a whole list route. An unknown value coerces to ``NONE`` with a
    warning, which fails closed: ``none`` passes no ``provisioned`` gate. Absent or empty is ``NONE``.
    """
    if isinstance(raw, IdentityStatus):
        return raw
    if not raw:
        return IdentityStatus.NONE
    try:
        return IdentityStatus(raw)
    except ValueError:
        logger.warning(
            "Unknown stored identity_status %r; coercing to %r (fails closed — "
            "the record will not pass any provisioned gate)",
            raw,
            IdentityStatus.NONE.value,
        )
        return IdentityStatus.NONE


STATUS_TO_LIFECYCLE: dict[str, LifecycleState] = {
    "DRAFT": LifecycleState.PROPOSED,
    "PENDING_APPROVAL": LifecycleState.PENDING_APPROVAL,
    "APPROVED": LifecycleState.APPROVED,
    "REJECTED": LifecycleState.REJECTED,
    "DEPRECATED": LifecycleState.DEPRECATED,
    "CREATING": LifecycleState.PROPOSED,
    "UPDATING": LifecycleState.APPROVED,
    "CREATE_FAILED": LifecycleState.PROPOSED,
    "UPDATE_FAILED": LifecycleState.APPROVED,
}

TRANSITION_TO_STATUS: dict[str, str] = {
    "approve": "APPROVED",
    "reject": "REJECTED",
    "deprecate": "DEPRECATED",
}


class UnknownRegistryStatusError(ValueError):
    """A registry record's status is not in ``STATUS_TO_LIFECYCLE``. AWS Agent Registry is a Preview
    service; a new or re-cased status fails loudly with context rather than as a bare ``KeyError``
    inside record hydration."""


def lifecycle_for_status(status: str) -> "LifecycleState":
    """The ``LifecycleState`` for a native registry status. Total over the native enum: the transient
    sync states collapse onto the nearest stable state, pre-live onto PROPOSED and post-live onto
    APPROVED; an unmapped value raises ``UnknownRegistryStatusError``."""
    try:
        return STATUS_TO_LIFECYCLE[status]
    except KeyError:
        raise UnknownRegistryStatusError(
            f"Unmapped AWS Agent Registry status {status!r}; known statuses: {sorted(STATUS_TO_LIFECYCLE)}"
        ) from None


ENVELOPE_SCHEMA_VERSION = 1

UNKNOWN_STAGE = "unknown"


def resolve_runtime_arns(agent) -> Dict[str, str]:
    """Every runtime an agent owns, as ``stage -> ARN``; the one place the map-else-scalar rule lives.

    A populated ``agent_arns`` wins outright: it is the complete inventory, and the buildspec writes
    ``agent_arn`` as a duplicate of the last stage's ARN, so unioning the scalar in would list one
    runtime twice and the delete cascade would call ``DeleteAgentRuntime`` on it twice. An absent or
    empty map with a scalar present is a legacy record and yields one entry keyed ``UNKNOWN_STAGE``,
    because the record does not know which stage that runtime belongs to and naming one would be a
    fabrication. Neither means nothing was ever provisioned: ``{}``.

    Duck-typed on purpose: the delete cascade reads a record that may be any object carrying the two
    fields, and ``getattr`` defaults make a partial record a non-error.
    """
    mapped = getattr(agent, "agent_arns", None) or {}
    if mapped:
        return dict(mapped)
    scalar = getattr(agent, "agent_arn", None)
    if scalar:
        return {UNKNOWN_STAGE: scalar}
    return {}


AWS_ARN_REGION_RE = re.compile(r"^[a-z]{2}-[a-z]+(?:-[a-z]+)?-\d$")


def is_harness_arn(arn: str | None) -> bool:
    """True iff ``arn`` names an AgentCore harness (resource segment starts with ``harness/``).

    A config agent's harness ARN lands in the same ``agent_arn`` / ``agent_arns`` slots a runtime's
    does, so the resource type is the only discriminator; every runtime-only AgentCore call checks
    this and skips a harness.
    """
    parts = (arn or "").split(":", 5)
    return len(parts) == 6 and parts[5].startswith("harness/")


def _validate_agent_arn_region(agent_arn: Optional[str]) -> Optional[str]:
    """The write-time twin of ``platform_adapters.aws``'s invoke-time ARN guard; both use
    ``AWS_ARN_REGION_RE``, so the two cannot drift into checking different shapes.

    ``agent_arn`` is caller-supplied at registration and on ``PUT /agents/{id}``; refusing a malformed
    or bad-region ARN here is a 422 that never reaches a runtime call. ``None`` or empty passes through:
    the field is optional, and "no ARN yet" is not malformed.
    """
    if not agent_arn:
        return agent_arn
    parts = agent_arn.split(":")
    if len(parts) < 4 or not parts[3] or not AWS_ARN_REGION_RE.fullmatch(parts[3]):
        raise ValueError("agent_arn is malformed or names an invalid AWS region")
    return agent_arn


class AgentBase(WireModel):
    """The fields the create and read models share; ``resolve_runtime_arns`` is the one reader of the
    ``agent_arns``-else-``agent_arn`` rule the two ARN fields describe."""

    model_config = ConfigDict(protected_namespaces=())

    name: str = Field(..., min_length=1, max_length=255)
    purpose: Optional[str] = Field(default="", max_length=4096)
    sponsor_oid: Optional[str] = None
    sponsor_email: Optional[str] = None
    business_unit: Optional[str] = None
    region: Optional[str] = None
    data_classification: Optional[DataClassification] = None
    platform: Optional[Platform] = None
    framework: Optional[str] = None
    model_id: Optional[str] = Field(
        default=None, description="The Bedrock model the runtime container invokes"
    )
    mcp_server_ids: List[str] = Field(default_factory=list, description="The MCP servers Governance granted")
    declared_mcp_server_ids: List[str] = Field(
        default_factory=list,
        description=(
            "The MCP servers the developer wired the agent to (the Studio proposal), distinct from "
            "mcp_server_ids; the runtime gets the union and Entra consent is the gate"
        ),
    )
    origin: Origin = Origin.REGISTERED
    endpoint_url: Optional[str] = None
    auth_type: AgentAuthType = AgentAuthType.NONE
    agent_arn: Optional[str] = Field(
        default=None,
        description=(
            "The AgentCore runtime ARN of whichever stage deployed last; AgentCore only, never a "
            "Databricks handle, because the delete cascade and the runtime-status probe parse it as a "
            "Bedrock ARN"
        ),
    )
    agent_arns: Dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Per-stage runtime ARNs, stage name to ARN. An agent owns one runtime per stage, so the "
            "scalar agent_arn can name only one; the buildspec writes both. Empty on every record "
            "written before the map existed, which readers treat as a legacy record and fall back to "
            "the scalar"
        ),
    )
    tenant_id: Optional[str] = Field(
        default=None, description="The owning tenant; optional on stored records, required on create"
    )
    published: bool = Field(default=False, description="Visible to every tenant")
    runtime_handle: Optional[str] = Field(
        default=None, description="Databricks app URL or serving-endpoint handle; the Databricks runtime leg"
    )
    runtime_kind: Optional[str] = Field(default=None, description="app or serving_endpoint")
    binding_mode: Optional[TenantBindingMode] = Field(
        default=None,
        description=(
            'Copied from the tenant at registration: "" (aws), federation, invoke_unavailable or '
            "sp_secret (dormant); a stored value outside the set hydrates to None"
        ),
    )
    databricks_sp_id: Optional[str] = Field(
        default=None, description="sp_secret mode: the per-agent service principal's application id"
    )
    databricks_sp_secret_arn: Optional[str] = Field(
        default=None,
        description=(
            "Secrets Manager ARN of the per-agent SP secret; the ARN only, the value never leaves "
            "Secrets Manager"
        ),
    )
    oauth2_app_client_id: Optional[str] = Field(
        default=None, description="The app's Databricks OAuth client id (federation mode)"
    )
    trace_delivery: Optional[Literal["auto", "manual"]] = Field(
        default=None,
        description=(
            "Whether AGP injects the OTLP env onto a runtime it does not own; the Langfuse project and "
            "key are always provisioned. None means auto"
        ),
    )


class AgentCreate(AgentBase):
    """Create payload. ``tenant_id`` is required on a new agent; the sponsor stays optional. Carries no
    service-written field (``project_id``, the identity ids, ``marketplace``): a body key of that name
    is dropped by pydantic's default ``extra="ignore"``, exactly as ``AgentUpdate`` drops it."""

    tenant_id: str = Field(..., description="The owning tenant")

    _validate_arn = field_validator("agent_arn")(_validate_agent_arn_region)


class AgentUpdate(WireModel):
    """Standalone update payload: every field optional, absent (None) means leave unchanged, driven
    through ``model_dump(exclude_none=True)``. Deliberately without ``agent_arns``: the map's only
    writers are the buildspec and ``persist_identity``, no route needs to patch it, and ``update()``'s
    read-modify-write preserves a field absent from the payload."""

    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    purpose: Optional[str] = Field(default=None, max_length=4096)
    sponsor_oid: Optional[str] = None
    sponsor_email: Optional[str] = None
    business_unit: Optional[str] = None
    region: Optional[str] = None
    data_classification: Optional[DataClassification] = None
    platform: Optional[Platform] = None
    framework: Optional[str] = None
    mcp_server_ids: Optional[List[str]] = None
    declared_mcp_server_ids: Optional[List[str]] = None
    origin: Optional[Origin] = None
    endpoint_url: Optional[str] = None
    auth_type: Optional[AgentAuthType] = None
    published: Optional[bool] = None
    agent_arn: Optional[str] = None
    _validate_arn = field_validator("agent_arn")(_validate_agent_arn_region)
    trace_delivery: Optional[Literal["auto", "manual"]] = None


class Agent(AgentBase):
    id: str = Field(description="The registry recordId")
    lifecycle_state: LifecycleState = Field(description="Derived from the native status by the service")
    entra_app_id: Optional[str] = None
    entra_api_app_id: Optional[str] = None
    entra_sp_id: Optional[str] = Field(
        default=None, description="The agent SP object id, for appRoleAssignedTo and oauth2PermissionGrants"
    )
    entra_app_audience: Optional[str] = Field(
        default=None,
        description=(
            "The per-agent token aud, equal to the AgentCore allowedAudience: the api://agp-agent-<id> "
            "URI, since per-agent apps set requestedAccessTokenVersion=2"
        ),
    )
    invoker_role_id: Optional[str] = Field(default=None, description="appRole GUID minted at provisioning")
    admin_role_id: Optional[str] = Field(default=None, description="appRole GUID minted at provisioning")
    identity_status: IdentityStatus = Field(
        default=IdentityStatus.NONE,
        description="The provisioning gate; coerced tolerantly on read because the buildspec writes it",
    )
    oauth2_credential_provider_name: Optional[str] = Field(
        default=None,
        description=(
            "Name of the per-agent MicrosoftOauth2 credential provider in AgentCore Identity's Token "
            "Vault; the reference agent reads it as CREDENTIAL_PROVIDER_NAME. A name, never the secret"
        ),
    )
    project_id: Optional[str] = Field(
        default=None,
        description=(
            "The project whose roles gate this agent's mutation routes, stamped by ProjectService at "
            "repo-materialize time; None for an agent registered directly, which stays tenant-gated"
        ),
    )
    langfuse_project_id: Optional[str] = Field(
        default=None, description="The agent's own Langfuse project; None when not provisioned"
    )
    langfuse_key_secret_name: Optional[str] = Field(
        default=None,
        description=(
            "Secrets Manager name holding the Langfuse public and secret key; the name, never the values"
        ),
    )
    marketplace: Optional[MarketplacePublication] = Field(
        default=None,
        description=(
            "The approved declared datasheet and its attestation, written only by "
            "AgentRegistryService.persist_marketplace from the approve path; None means never "
            "published. Distinct from the published flag, which is cross-tenant visibility"
        ),
    )
    bound_stage: Optional[str] = Field(
        default=None,
        description=(
            "The Databricks workspace stage key this agent was bound in, written by provisioning; a "
            "Databricks Apps hostname carries no workspace identity, so the invoke resolver prefers it "
            "and falls back to the distinct-workspace heuristic for records without it"
        ),
    )
    created_at: datetime
    updated_at: datetime
    created_by: Optional[str] = None

    @property
    def is_agentcore(self) -> bool:
        """The AgentCore provisioning gate: a runtime ARN, Entra auth and the Bedrock platform. The one
        predicate ``agent_registry_service.create()`` and ``agent_identity_service.provision()`` share."""
        return (
            bool(self.agent_arn)
            and self.auth_type == AgentAuthType.ENTRA
            and self.platform == Platform.AWS_BEDROCK
        )

    @property
    def is_databricks_governed(self) -> bool:
        """The Databricks provisioning and invoke gate: a runtime handle, Entra auth and the Databricks
        platform; the structural mirror of ``is_agentcore``, and because the two demand different
        platforms they can never both be True, which makes the create hook's dispatch a dispatch and
        not a race. Deliberately independent of ``binding_mode``: which credential path provisioning
        takes is not whether the agent is governed at all."""
        return (
            bool(self.runtime_handle)
            and self.auth_type == AgentAuthType.ENTRA
            and self.platform == Platform.DATABRICKS
        )

    def runtime_arns(self) -> Dict[str, str]:
        """Every runtime this agent owns, as ``stage -> ARN``; delegates to ``resolve_runtime_arns``."""
        return resolve_runtime_arns(self)

    def to_envelope(self) -> dict:
        """The governance JSON stored in the record's ``descriptors.custom.data``.

        Excludes the native record fields (``name``, ``purpose`` as the record description, the
        timestamps) and the derived ``lifecycle_state``. Enums are serialized as their ``.value``,
        with an ``isinstance`` guard because pydantic does not validate assignment and a raw string
        must serialize through rather than fail on ``.value``; nested models dump with
        ``mode="json"``. Every key is written on every save, ``None`` or an empty container when
        unset: omitting one would leave a stale value in place on the next read-modify-write and
        would drop the ``agent_arns`` map the buildspec patches in on the next identity persist.
        """
        return {
            "schema_version": ENVELOPE_SCHEMA_VERSION,
            "agent_id": self.id,
            "sponsor_oid": self.sponsor_oid,
            "sponsor_email": self.sponsor_email,
            "business_unit": self.business_unit,
            "region": self.region,
            "data_classification": self.data_classification.value if self.data_classification else None,
            "platform": self.platform.value if self.platform else None,
            "framework": self.framework,
            "model_id": self.model_id,
            "mcp_server_ids": list(self.mcp_server_ids),
            "declared_mcp_server_ids": list(self.declared_mcp_server_ids),
            "entra_app_id": self.entra_app_id,
            "entra_api_app_id": self.entra_api_app_id,
            "origin": self.origin.value if isinstance(self.origin, Origin) else self.origin,
            "created_by": self.created_by,
            "endpoint_url": self.endpoint_url,
            "auth_type": self.auth_type.value
            if isinstance(self.auth_type, AgentAuthType)
            else self.auth_type,
            "agent_arn": self.agent_arn,
            "agent_arns": dict(self.agent_arns),
            "entra_sp_id": self.entra_sp_id,
            "entra_app_audience": self.entra_app_audience,
            "invoker_role_id": self.invoker_role_id,
            "admin_role_id": self.admin_role_id,
            "identity_status": (
                self.identity_status.value
                if isinstance(self.identity_status, IdentityStatus)
                else self.identity_status
            ),
            "oauth2_credential_provider_name": self.oauth2_credential_provider_name,
            "tenant_id": self.tenant_id,
            "published": self.published,
            "project_id": self.project_id,
            "langfuse_project_id": self.langfuse_project_id,
            "langfuse_key_secret_name": self.langfuse_key_secret_name,
            "runtime_handle": self.runtime_handle,
            "runtime_kind": self.runtime_kind,
            "binding_mode": self.binding_mode,
            "databricks_sp_id": self.databricks_sp_id,
            "databricks_sp_secret_arn": self.databricks_sp_secret_arn,
            "oauth2_app_client_id": self.oauth2_app_client_id,
            "marketplace": self.marketplace.model_dump(mode="json") if self.marketplace else None,
            "bound_stage": self.bound_stage,
            "trace_delivery": self.trace_delivery,
        }

    @model_validator(mode="before")
    @classmethod
    def _fold_binding_mode(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        value = data.get("binding_mode")
        if value is None or value in get_args(TenantBindingMode):
            return data
        logger.warning(
            "%s %r is outside its set on %s; folded to %r", "binding_mode", value, data.get("id"), None
        )
        return {**data, "binding_mode": None}

    @classmethod
    def from_record(cls, record: dict, envelope: dict) -> "Agent":
        """Build an ``Agent`` from a GetRegistryRecord-style ``record`` and its parsed envelope.

        The native fields (``recordId``, ``displayName`` else ``name``, ``description``, ``status``,
        the timestamps) are authoritative; everything else comes from the envelope, where a missing
        key hydrates to the field's default. Enum values arrive as plain strings and pydantic coerces
        them, except ``identity_status``, which ``coerce_identity_status`` tolerates. ``displayName``
        is what the registry shows and what ``create`` writes; ``name`` is the fallback for records
        written before it existed.
        """
        return cls(
            id=record["recordId"],
            name=record.get("displayName") or record["name"],
            purpose=record.get("description", "") or "",
            lifecycle_state=lifecycle_for_status(record["status"]),
            created_at=record["createdAt"],
            updated_at=record["updatedAt"],
            sponsor_oid=envelope.get("sponsor_oid"),
            sponsor_email=envelope.get("sponsor_email"),
            business_unit=envelope.get("business_unit"),
            region=envelope.get("region"),
            data_classification=envelope.get("data_classification"),
            platform=envelope.get("platform"),
            framework=envelope.get("framework"),
            model_id=envelope.get("model_id"),
            mcp_server_ids=envelope.get("mcp_server_ids") or [],
            declared_mcp_server_ids=envelope.get("declared_mcp_server_ids") or [],
            entra_app_id=envelope.get("entra_app_id"),
            entra_api_app_id=envelope.get("entra_api_app_id"),
            origin=envelope.get("origin") or Origin.REGISTERED,
            created_by=envelope.get("created_by"),
            endpoint_url=envelope.get("endpoint_url"),
            auth_type=envelope.get("auth_type") or AgentAuthType.NONE,
            agent_arn=envelope.get("agent_arn"),
            agent_arns=envelope.get("agent_arns") or {},
            entra_sp_id=envelope.get("entra_sp_id"),
            entra_app_audience=envelope.get("entra_app_audience"),
            invoker_role_id=envelope.get("invoker_role_id"),
            admin_role_id=envelope.get("admin_role_id"),
            identity_status=coerce_identity_status(envelope.get("identity_status")),
            oauth2_credential_provider_name=envelope.get("oauth2_credential_provider_name"),
            tenant_id=envelope.get("tenant_id"),
            published=envelope.get("published") or False,
            project_id=envelope.get("project_id"),
            langfuse_project_id=envelope.get("langfuse_project_id"),
            langfuse_key_secret_name=envelope.get("langfuse_key_secret_name"),
            runtime_handle=envelope.get("runtime_handle"),
            runtime_kind=envelope.get("runtime_kind"),
            binding_mode=envelope.get("binding_mode"),
            databricks_sp_id=envelope.get("databricks_sp_id"),
            databricks_sp_secret_arn=envelope.get("databricks_sp_secret_arn"),
            oauth2_app_client_id=envelope.get("oauth2_app_client_id"),
            marketplace=(
                MarketplacePublication.model_validate(envelope["marketplace"])
                if envelope.get("marketplace")
                else None
            ),
            bound_stage=envelope.get("bound_stage"),
            trace_delivery=envelope.get("trace_delivery"),
        )


def is_databricks_governed_agent(agent: Agent) -> bool:
    """The Databricks provisioning and invoke gate as a module function; delegates to
    ``Agent.is_databricks_governed``. Lives beside the model because the branch is dispatched from the
    route and from ``agent_identity_service.provision`` alike, and a predicate in either would make
    the other import across a seam it does not otherwise use."""
    return agent.is_databricks_governed


RuntimeStatusValue = Literal["ready", "creating", "updating", "failed", "not_deployed", "unknown"]
RUNTIME_STATUSES = get_args(RuntimeStatusValue)


class RuntimeStatus(WireModel):
    """A point-in-time read of one agent's AgentCore Runtime; not persisted and not in the envelope,
    because a stored copy would go stale silently.

    ``unknown`` is distinct from ``failed``: an unreachable or denying control plane is not a broken
    runtime, and conflating them would report a probe failure as an outage. ``stage``
    names a real stage when the record's ``agent_arns`` map attributes the runtime to one and is
    ``UNKNOWN_STAGE`` for a legacy scalar-only record; the frontend treats a named stage as
    attributable evidence, so it is never guessed. ``detail`` is a safe short hint only: never a
    token, a raw upstream body or an account id; ``runtime_arn`` is one of the agent's own ARNs, which
    every agent route already returns.
    """

    agent_id: str
    stage: str
    status: RuntimeStatusValue
    runtime_arn: Optional[str] = None
    image_tag: Optional[str] = None
    checked_at: str
    detail: Optional[str] = None
