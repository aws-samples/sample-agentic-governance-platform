"""MCP Server domain models and the envelope / ``server.json`` (de)serialization.

An MCP server is an ``MCP``-type AWS Agent Registry record in the dedicated MCP registry. Unlike the
agent's Custom record the payload is schema-validated: ``descriptors.mcpServer.data`` is a stringified
``server.json`` and ``descriptors.mcpServer.additionalData.tools.data`` a stringified
``{"tools": [...]}``. Governance metadata rides inside ``server.json`` at
``_meta["com.agp/governance"]``, the envelope; ``McpServer.to_envelope`` and ``McpServer.from_record``
are the only two places that read or write it, so a move off ``_meta`` is a one-method change.

The lifecycle machinery is the agent's, imported and re-exported from ``models.agent`` rather than
redefined, so ``from models.mcp_server import LifecycleState`` is the same object. The same envelope
discipline as ``models.agent`` applies: service-written fields live on ``McpServer`` and the envelope
only, every key is written on every save (``None`` when unset) and a missing stored key hydrates to
the default, which keeps ``ENVELOPE_SCHEMA_VERSION`` at 1.
"""

from __future__ import annotations

import logging
from datetime import datetime
from enum import Enum
from typing import List, Literal, Optional, get_args

from pydantic import Field, ValidationInfo, field_validator

from models.agent import (
    ENVELOPE_SCHEMA_VERSION,
    STATUS_TO_LIFECYCLE,
    TRANSITION_TO_STATUS,
    DataClassification,
    IdentityStatus,
    LifecycleState,
    UnknownRegistryStatusError,
    coerce_identity_status,
    lifecycle_for_status,
)
from models.base import WireModel
from models.marketplace import MarketplacePublication

__all__ = [
    "ENVELOPE_SCHEMA_VERSION",
    "MAX_SERVER_JSON_DESCRIPTION",
    "STATUS_TO_LIFECYCLE",
    "TRANSITION_TO_STATUS",
    "CedarEnforcementMode",
    "DataClassification",
    "EnforcementState",
    "IdentityStatus",
    "Kind",
    "LifecycleState",
    "McpServer",
    "McpServerBase",
    "McpServerCreate",
    "McpServerUpdate",
    "McpTool",
    "UnknownRegistryStatusError",
    "coerce_identity_status",
    "lifecycle_for_status",
]

logger = logging.getLogger(__name__)

CedarEnforcementMode = Literal["none", "log_only", "enforce"]


class Kind(str, Enum):
    """The MCP server's discriminator; gates the Policies tab. ``RUNTIME`` is an AgentCore Runtime
    serving MCP (``serverProtocol=MCP``)."""

    GATEWAY = "gateway"
    RUNTIME = "runtime"
    STANDARD = "standard"


class McpTool(WireModel):
    """A declared tool. ``input_schema`` is a JSON Schema object, snake_case on the model and
    ``inputSchema`` in the MCP tools payload (``McpServer.tools_as_mcp``)."""

    name: str = Field(..., min_length=1)
    description: Optional[str] = ""
    input_schema: dict = Field(default_factory=dict)


MAX_SERVER_JSON_DESCRIPTION = 100


class McpServerBase(WireModel):
    name: str = Field(..., min_length=1, max_length=255, description="The native record name")
    description: Optional[str] = Field(default="", max_length=4096)
    kind: Kind = Kind.STANDARD
    owner_oid: Optional[str] = None
    owner_email: Optional[str] = None
    business_unit: Optional[str] = None
    region: Optional[str] = Field(
        default=None, description="Business or data region (DE, EU, ...), not an AWS region"
    )
    data_classification: Optional[DataClassification] = None
    endpoint_url: Optional[str] = Field(default=None, description="Written to server.json remotes[].url")
    version: str = Field(default="1.0.0", description="Written to server.json version")
    available_tools: List[McpTool] = Field(
        default_factory=list, description="Written to mcpServer.additionalData.tools"
    )
    gateway_arn: Optional[str] = None
    runtime_arn: Optional[str] = Field(default=None, description="For kind=runtime; parallel to gateway_arn")
    tenant_id: Optional[str] = Field(
        default=None, description="The owning tenant; optional on stored records, required on create"
    )
    published: bool = Field(default=False, description="Visible to every tenant")
    shared: bool = Field(default=False, description="Platform-shared MCP server, visible to every tenant")


class McpServerCreate(McpServerBase):
    """Create payload. ``tenant_id`` is required on a new MCP server. ``description`` is capped at the
    registry's ``server.json`` limit on the write side only, so its opaque schema error becomes a 422
    naming the field while a stored record holding a longer description still hydrates."""

    description: Optional[str] = Field(default="", max_length=MAX_SERVER_JSON_DESCRIPTION)
    tenant_id: str = Field(..., description="The owning tenant")


class McpServerUpdate(WireModel):
    """Standalone update payload: every field optional, absent (None) means leave unchanged, driven
    through ``model_dump(exclude_none=True)``. ``shared`` is settable only by an admin, which the
    route enforces."""

    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    description: Optional[str] = Field(default=None, max_length=MAX_SERVER_JSON_DESCRIPTION)
    kind: Optional[Kind] = None
    owner_oid: Optional[str] = None
    owner_email: Optional[str] = None
    business_unit: Optional[str] = None
    region: Optional[str] = None
    data_classification: Optional[DataClassification] = None
    endpoint_url: Optional[str] = None
    version: Optional[str] = None
    available_tools: Optional[List[McpTool]] = None
    gateway_arn: Optional[str] = None
    runtime_arn: Optional[str] = None
    published: Optional[bool] = None
    shared: Optional[bool] = None


class McpServer(McpServerBase):
    """The read model. The Entra identity block, the Cedar policy-engine block, ``marketplace`` and
    the repo-ownership pair are service-written: set by the provisioning, Cedar, marketplace-approve
    and repo-materialize paths respectively, never by a request body, so they live here and on the
    envelope only."""

    id: str = Field(description="The registry recordId")
    lifecycle_state: LifecycleState = Field(description="Derived from the native status by the service")
    entra_app_id: Optional[str] = None
    entra_sp_id: Optional[str] = Field(
        default=None, description="The MCP SP object id, for appRoleAssignedTo and oauth2PermissionGrants"
    )
    entra_app_audience: Optional[str] = Field(
        default=None, description="The MCP token aud, equal to the authorizer allowedAudience (may be a GUID)"
    )
    invoker_role_id: Optional[str] = Field(
        default=None, description="appRole GUID (allowedMemberTypes Application)"
    )
    admin_role_id: Optional[str] = None
    gateway_id: Optional[str] = Field(
        default=None, description="The short gatewayId parsed from gateway_arn, for control calls"
    )
    gateway_url: Optional[str] = Field(
        default=None, description="The verbatim MCP endpoint from GetGateway; the scan and invoke target"
    )
    identity_status: IdentityStatus = Field(
        default=IdentityStatus.NONE,
        description="The provisioning gate; coerced tolerantly on read because the buildspec writes it",
    )
    cedar_policy_engine_id: Optional[str] = Field(
        default=None, description="The Policy Engine id, for policy CRUD under /policy-engines/{id}/"
    )
    cedar_policy_engine_arn: Optional[str] = Field(
        default=None, description="The Policy Engine ARN, for the gateway policyEngineConfiguration.arn"
    )
    cedar_enforcement_mode: CedarEnforcementMode = Field(
        default="none",
        description="The engine mode; a stored value outside the set reads as none, with a warning",
    )
    marketplace: Optional[MarketplacePublication] = Field(
        default=None,
        description=(
            "The approved declared datasheet and its attestation, written only by "
            "McpServerRegistryService.persist_marketplace from the approve path; None means never "
            "published. Distinct from the published flag, which is cross-tenant visibility"
        ),
    )
    project_id: Optional[str] = Field(
        default=None,
        description="The project whose repo materialized this record; None when registered directly",
    )
    repo_id: Optional[str] = Field(
        default=None,
        description="The repository that materialized this record; None when registered directly",
    )
    created_at: datetime
    updated_at: datetime
    created_by: Optional[str] = None

    @field_validator("cedar_enforcement_mode", mode="before")
    @classmethod
    def _fold_enforcement_mode(cls, value: object, info: ValidationInfo) -> object:
        if value in get_args(CedarEnforcementMode):
            return value
        logger.warning(
            "%s %r is outside its set on %s; folded to %r",
            "cedar_enforcement_mode",
            value,
            info.data.get("id"),
            "none",
        )
        return "none"

    @property
    def is_repo_owned(self) -> bool:
        """True when a repo built this record (a plain property, not a wire field)."""
        return self.repo_id is not None

    def to_envelope(self) -> dict:
        """The governance dict stored at ``server.json`` ``_meta["com.agp/governance"]``.

        Excludes the native-record and validated ``server.json`` fields (``name``, ``description``,
        ``version``, ``endpoint_url``, ``available_tools``) and the derived ``lifecycle_state``. Base
        fields are not carried generically, so ``runtime_arn``, ``gateway_arn`` and the three tenant
        fields are listed by hand to round-trip. Enums serialize as ``.value`` with an ``isinstance``
        guard, since pydantic does not validate assignment; ``marketplace`` dumps with ``mode="json"``
        so the envelope stays ``json.dumps``-able. Every key is written on every save, ``None`` when
        unset, so a previously stored value cannot survive an unpublish or outlive a cleared field.
        """
        return {
            "schema_version": ENVELOPE_SCHEMA_VERSION,
            "mcp_server_id": self.id,
            "kind": self.kind.value if isinstance(self.kind, Kind) else self.kind,
            "owner_oid": self.owner_oid,
            "owner_email": self.owner_email,
            "business_unit": self.business_unit,
            "region": self.region,
            "data_classification": self.data_classification.value if self.data_classification else None,
            "entra_app_id": self.entra_app_id,
            "entra_sp_id": self.entra_sp_id,
            "entra_app_audience": self.entra_app_audience,
            "invoker_role_id": self.invoker_role_id,
            "admin_role_id": self.admin_role_id,
            "gateway_id": self.gateway_id,
            "gateway_url": self.gateway_url,
            "identity_status": (
                self.identity_status.value
                if isinstance(self.identity_status, IdentityStatus)
                else self.identity_status
            ),
            "gateway_arn": self.gateway_arn,
            "runtime_arn": self.runtime_arn,
            "cedar_policy_engine_id": self.cedar_policy_engine_id,
            "cedar_policy_engine_arn": self.cedar_policy_engine_arn,
            "cedar_enforcement_mode": self.cedar_enforcement_mode,
            "tenant_id": self.tenant_id,
            "published": self.published,
            "shared": self.shared,
            "marketplace": self.marketplace.model_dump(mode="json") if self.marketplace else None,
            "project_id": self.project_id,
            "repo_id": self.repo_id,
            "created_by": self.created_by,
        }

    def to_server_json(self) -> dict:
        """The validated ``server.json``: ``name`` namespaced under ``agp/`` (a bare name is rejected),
        ``remotes`` present only when ``endpoint_url`` is set."""
        server_json: dict = {
            "name": f"agp/{self.name}",
            "description": self.description or "",
            "version": self.version or "1.0.0",
            "_meta": {"com.agp/governance": self.to_envelope()},
        }
        if self.endpoint_url:
            server_json["remotes"] = [{"type": "streamable-http", "url": self.endpoint_url}]
        return server_json

    def tools_as_mcp(self) -> list[dict]:
        """The MCP tools payload: ``inputSchema`` (camelCase) on the wire, ``input_schema`` on the model."""
        return [
            {"name": t.name, "description": t.description or "", "inputSchema": t.input_schema}
            for t in self.available_tools
        ]

    @classmethod
    def from_record(cls, record: dict, server_json: dict, tools: list[dict]) -> "McpServer":
        """Build an ``McpServer`` from a GetRegistryRecord-style native ``record``, the parsed
        ``descriptors.mcpServer.data`` (``server_json``) and the parsed
        ``descriptors.mcpServer.additionalData.tools.data["tools"]`` (``tools``, may be empty).

        Native fields are authoritative, with ``displayName`` (what the registry shows and what
        ``create`` writes) preferred over ``name`` so records written before it existed still hydrate;
        ``description``, ``version`` and the endpoint come from ``server.json``; governance comes from
        the ``_meta`` envelope, where a missing key hydrates to the field's default. Enum values arrive
        as plain strings and pydantic coerces them, so an invalid one is a ``ValidationError`` rather
        than an opaque ``ValueError``; ``identity_status`` is coerced tolerantly instead and a
        ``cedar_enforcement_mode`` outside its set folds to ``none`` with a warning. A present but
        structurally invalid ``marketplace`` block raises here, and the service's hydrate seam
        translates that into ``MalformedMcpRecordError`` so one bad record cannot 500 the list view.
        """
        envelope = server_json.get("_meta", {}).get("com.agp/governance", {})

        remotes = server_json.get("remotes") or []
        endpoint_url = remotes[0].get("url") if remotes else None

        return cls(
            id=record["recordId"],
            name=record.get("displayName") or record["name"],
            description=server_json.get("description", "") or "",
            kind=envelope.get("kind") or Kind.STANDARD,
            owner_oid=envelope.get("owner_oid"),
            owner_email=envelope.get("owner_email"),
            business_unit=envelope.get("business_unit"),
            region=envelope.get("region"),
            data_classification=envelope.get("data_classification"),
            endpoint_url=endpoint_url,
            version=server_json.get("version") or "1.0.0",
            available_tools=[
                McpTool(
                    name=t.get("name", ""),
                    description=t.get("description", "") or "",
                    input_schema=t.get("inputSchema") or {},
                )
                for t in tools
            ],
            gateway_arn=envelope.get("gateway_arn"),
            runtime_arn=envelope.get("runtime_arn"),
            cedar_policy_engine_id=envelope.get("cedar_policy_engine_id"),
            cedar_policy_engine_arn=envelope.get("cedar_policy_engine_arn"),
            cedar_enforcement_mode=envelope.get("cedar_enforcement_mode") or "none",
            tenant_id=envelope.get("tenant_id"),
            published=envelope.get("published") or False,
            shared=envelope.get("shared") or False,
            lifecycle_state=lifecycle_for_status(record["status"]),
            entra_app_id=envelope.get("entra_app_id"),
            entra_sp_id=envelope.get("entra_sp_id"),
            entra_app_audience=envelope.get("entra_app_audience"),
            invoker_role_id=envelope.get("invoker_role_id"),
            admin_role_id=envelope.get("admin_role_id"),
            gateway_id=envelope.get("gateway_id"),
            gateway_url=envelope.get("gateway_url"),
            identity_status=coerce_identity_status(envelope.get("identity_status")),
            marketplace=(
                MarketplacePublication.model_validate(envelope["marketplace"])
                if envelope.get("marketplace")
                else None
            ),
            project_id=envelope.get("project_id"),
            repo_id=envelope.get("repo_id"),
            created_at=record["createdAt"],
            updated_at=record["updatedAt"],
            created_by=envelope.get("created_by"),
        )


class EnforcementState(WireModel):
    """The body of ``PUT /mcp-servers/{mcp_id}/policy-enforcement``: the mode the engine now runs in."""

    enforcement_mode: CedarEnforcementMode
