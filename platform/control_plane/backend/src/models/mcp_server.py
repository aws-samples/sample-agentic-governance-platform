"""MCP Server domain models + envelope/server.json (de)serialization (Epic 5, Task 1).

An MCP server is an `MCP`-type **AWS Agent Registry** record in the dedicated
`agp-mcp-servers` registry (research §0, §7). Unlike E4's CUSTOM record,
the payload is schema-validated: `descriptors.mcpServer.data` is a stringified
`server.json` and `descriptors.mcpServer.additionalData.tools.data` is a stringified
`{"tools": [...]}` (E32 renamed both — research §2). Governance metadata rides inside `server.json`
`_meta["com.agp/governance"]` — the "envelope" (research §3, §3.1) —
which is the SINGLE place that reads/writes governance, so a future move off
`_meta` is a one-method change.

The lifecycle machinery is **identical to E4** (research §5), so it is imported
and re-exported from `models.agent` rather than redefined. Only the MCP-specific
pieces (`Kind`, `McpTool`, the four `McpServer*` classes) live here.

Mirrors `models.agent`'s Base/Create/Update/read split + envelope idiom.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field

# Reuse, do NOT redefine — research §5 says MCP lifecycle is identical to E4's.
# Re-exported so `from models.mcp_server import LifecycleState` is the SAME object.
from models.agent import (  # noqa: F401  (re-exported)
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

# The marketplace publication block carried on the envelope (Epic 33 Amendment 1 / C8).
# Safe in either direction: `models.marketplace` imports nothing from this module.
from models.marketplace import MarketplacePublication


# ---------------------------------------------------------------------------
# MCP-specific enums / sub-models
# ---------------------------------------------------------------------------

class Kind(str, Enum):
    """Discriminator (research §4) — mirrors E4's `origin`. Gates the Policies tab."""

    GATEWAY = "gateway"
    RUNTIME = "runtime"      # AgentCore Runtime-MCP (serverProtocol=MCP) — Epic 7
    STANDARD = "standard"


class McpTool(BaseModel):
    """A declared tool. `input_schema` is a JSON Schema object (snake_case on the
    model; serialized as `inputSchema` in the MCP tools payload — see tools_as_mcp)."""

    name: str = Field(..., min_length=1)
    description: Optional[str] = ""
    input_schema: dict = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Models (mirror agent.py's Base/Create/Update/read split)
# ---------------------------------------------------------------------------

class McpServerBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)  # native record name [A-Za-z0-9_\-./]
    description: Optional[str] = Field(default="", max_length=4096)
    kind: Kind = Kind.STANDARD  # discriminator — gates the Policies tab
    owner_oid: Optional[str] = None
    owner_email: Optional[str] = None
    business_unit: Optional[str] = None
    region: Optional[str] = None  # business/data region (DE/EU/…), NOT AWS region
    data_classification: Optional[DataClassification] = None
    endpoint_url: Optional[str] = None  # -> server.json remotes[].url
    version: str = Field(default="1.0.0")  # -> server.json version
    available_tools: List[McpTool] = Field(default_factory=list)  # -> mcpServer.additionalData.tools
    gateway_arn: Optional[str] = None  # reserved for deferred Gateway work; null in E5
    runtime_arn: Optional[str] = None  # for kind=runtime (Epic 7); parallel to gateway_arn
    # Multi-tenancy (Epic 24) — owning tenant + cross-tenant publish/share flags.
    # Optional on Base (pre-E24 records hydrate as None); REQUIRED on McpServerCreate.
    tenant_id: Optional[str] = None
    published: bool = False
    shared: bool = False  # platform-shared MCP: visible to every tenant (spec §5)


class McpServerCreate(McpServerBase):
    """Create payload. `tenant_id` is REQUIRED on new MCP servers (E24 spec §6)."""

    tenant_id: str


class McpServerUpdate(BaseModel):
    """Standalone — every field Optional (drives model_dump(exclude_none=True))."""

    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    description: Optional[str] = Field(default=None, max_length=4096)
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
    # Multi-tenancy (Epic 24/T5) — the publish route flips this via the existing
    # read-modify-write update path; absent (None) means "leave unchanged".
    published: Optional[bool] = None
    # `shared` is settable only by ADMIN (route-enforced); absent (None) here means
    # "leave unchanged", matching every other Update field.
    shared: Optional[bool] = None


class McpServer(McpServerBase):
    id: str  # the registry recordId — assigned by the registry (no default)
    lifecycle_state: LifecycleState  # derived from native status, set by the service
    entra_app_id: Optional[str] = None  # set in E7
    # Entra identity provisioning (Epic 7) — SERVICE-WRITTEN (like entra_app_id), set by
    # the mcp_identity_service provisioning hook, never user-supplied; so on McpServer +
    # envelope only, NOT Base/Create/Update. Mirrors E6's Agent block field-for-field.
    # Research §6.1 / §6.2.
    entra_sp_id: Optional[str] = None          # MCP SP object id — for every appRoleAssignedTo / oauth2PermissionGrants call
    entra_app_audience: Optional[str] = None   # the MCP token aud == authorizer allowedAudience (may be GUID)
    invoker_role_id: Optional[str] = None      # appRole GUID (allowedMemberTypes Application)
    admin_role_id: Optional[str] = None
    gateway_id: Optional[str] = None           # short gatewayId (parsed from gateway_arn) — for control calls
    gateway_url: Optional[str] = None          # the verbatim MCP endpoint from GetGateway — the scan/invoke target
    # The operational gate — a PINNED enum since E36/T20, the SAME `IdentityStatus`
    # object as the agent side (imported, never redefined). Tolerant on the READ side
    # via `coerce_identity_status` in `from_record`.
    identity_status: IdentityStatus = IdentityStatus.NONE
    # Cedar policy engine (Epic 8) — SERVICE-WRITTEN (like the E7 identity block), set by
    # mcp_cedar_service when a Policy Engine is created/attached, never user-supplied; so on
    # McpServer + envelope only, NOT Base/Create/Update. Additive + tolerant of pre-E8 envelopes.
    cedar_policy_engine_id: Optional[str] = None     # the Policy Engine id (for policy CRUD: /policy-engines/{id}/...)
    cedar_policy_engine_arn: Optional[str] = None    # its ARN (for the gateway policyEngineConfiguration.arn)
    cedar_enforcement_mode: str = "none"             # none | log_only | enforce  (plain str — tolerant, like identity_status)
    # Marketplace publication (Epic 33 Amendment 1 / C8) — the approved DECLARED datasheet
    # plus its attestation (who declared it, when). SERVICE-WRITTEN for the same reason the
    # E7 identity block above is (and the agent-side `project_id` convention — see
    # `models/agent.py`): a marketplace datasheet is an ATTESTATION, so it must never be
    # settable by a REQUEST BODY, which would let a publisher self-certify an
    # "admin-approved" SLA tier and compliance list. It therefore lives on `McpServer` +
    # the envelope ONLY (NOT McpServerBase/Create/Update) and is persisted through the
    # dedicated `McpServerRegistryService.persist_marketplace`, whose only caller is the
    # marketplace approve/unpublish path. None = never published (and every pre-E33 record).
    #
    # NOT the E24 `published` flag on Base: that is the cross-TENANT visibility flag flipped
    # by `PUT /mcp-servers/{id}/publish`. Marketplace publication is a different feature with
    # a different approver, which is why every name here carries the `marketplace` prefix.
    marketplace: Optional[MarketplacePublication] = None
    created_at: datetime
    updated_at: datetime
    created_by: Optional[str] = None

    # -- governance envelope (de)serialization ------------------------------

    def to_envelope(self) -> dict:
        """Return the research-§3.1 governance dict stored at server.json
        `_meta["com.agp/governance"]`.

        EXCLUDES the native-record / validated-server.json fields (`name`,
        `description`, `version`, `endpoint_url`, `available_tools`) and the
        derived `lifecycle_state`. Enums are serialized as their `.value` so the
        dict is JSON-safe (json.dumps-able).
        """
        return {
            "schema_version": ENVELOPE_SCHEMA_VERSION,
            "mcp_server_id": self.id,  # mirror of recordId for debug convenience
            "kind": self.kind.value if isinstance(self.kind, Kind) else self.kind,
            "owner_oid": self.owner_oid,
            "owner_email": self.owner_email,
            "business_unit": self.business_unit,
            "region": self.region,
            "data_classification": self.data_classification.value if self.data_classification else None,
            "entra_app_id": self.entra_app_id,
            # Entra identity + gateway/runtime (Epic 7) — additive; pre-E7 envelopes lack these keys
            "entra_sp_id": self.entra_sp_id,
            "entra_app_audience": self.entra_app_audience,
            "invoker_role_id": self.invoker_role_id,
            "admin_role_id": self.admin_role_id,
            "gateway_id": self.gateway_id,
            "gateway_url": self.gateway_url,
            # `.value` — this envelope is json.dumps-ed into `server.json`'s `_meta`, so a
            # stored enum repr ("IdentityStatus.NONE") would be a data-corruption bug. The
            # isinstance guard mirrors the agent side and stays defensive: the live writers
            # all assign a member, but pydantic does not validate assignment, so a raw
            # string must serialize through rather than AttributeError on `.value`.
            "identity_status": (
                self.identity_status.value
                if isinstance(self.identity_status, IdentityStatus)
                else self.identity_status
            ),
            "gateway_arn": self.gateway_arn,
            # runtime_arn is a Base field, but to_envelope serializes fields explicitly
            # (Base fields are NOT carried generically), so it must be listed here to
            # round-trip — parallel to the gateway_arn handle above.
            "runtime_arn": self.runtime_arn,
            # Cedar policy engine (Epic 8) — additive; pre-E8 envelopes lack these keys
            "cedar_policy_engine_id": self.cedar_policy_engine_id,
            "cedar_policy_engine_arn": self.cedar_policy_engine_arn,
            "cedar_enforcement_mode": self.cedar_enforcement_mode,
            # Multi-tenancy (Epic 24) — additive; pre-E24 envelopes lack these keys.
            # Base fields are NOT carried generically (see runtime_arn above), so the
            # three tenant fields MUST be serialized here by hand to round-trip.
            "tenant_id": self.tenant_id,
            "published": self.published,
            "shared": self.shared,
            # Marketplace publication (Epic 33 Amendment 1) — additive; pre-E33 envelopes
            # lack this key. Dumped with mode="json" so the nested datetime becomes a string
            # and the envelope stays json.dumps-able (it is stringified into the descriptor).
            # Written as an explicit null when there is no block: omitting the key would
            # leave a stale block in place on the next read.
            "marketplace": self.marketplace.model_dump(mode="json") if self.marketplace else None,
            "created_by": self.created_by,
        }

    # -- server.json + tools builders (research §3, §8) ---------------------

    def to_server_json(self) -> dict:
        """Build the validated server.json (research §3). `name` is namespaced —
        a bare name is rejected. `remotes` is included ONLY when `endpoint_url`
        is set (the key is omitted entirely when null)."""
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
        """The MCP tools payload — note `inputSchema` (camelCase) on the wire vs
        `input_schema` (snake) on the McpTool model."""
        return [
            {"name": t.name, "description": t.description or "", "inputSchema": t.input_schema}
            for t in self.available_tools
        ]

    # -- record hydration ----------------------------------------------------

    @classmethod
    def from_record(cls, record: dict, server_json: dict, tools: list[dict]) -> "McpServer":
        """Build an McpServer from a GetRegistryRecord-style native `record`, the
        parsed `descriptors.mcpServer.data` (`server_json`), and the parsed
        `descriptors.mcpServer.additionalData.tools.data["tools"]` (`tools`, may be []).

        Native fields (recordId/name/displayName/status/createdAt/updatedAt) are
        authoritative; description/version/endpoint come from server.json; governance
        comes from the `_meta` envelope. Enum values arrive as plain strings (from JSON)
        and are coerced by Pydantic. NOTE: `name` comes from the NATIVE record, not
        server.json's namespaced name.

        E32 added the native `displayName`, which is what the registry now shows and what
        our `create` writes, so it is preferred here. `name` remains the fall-back so
        records written before the migration (which have no `displayName`) still hydrate
        rather than KeyError-ing the whole list view.
        """
        envelope = server_json.get("_meta", {}).get("com.agp/governance", {})

        remotes = server_json.get("remotes") or []
        endpoint_url = remotes[0].get("url") if remotes else None

        return cls(
            id=record["recordId"],
            name=record.get("displayName") or record["name"],
            description=server_json.get("description", "") or "",
            # enum values arrive as plain strings (from JSON); pass them raw and let
            # Pydantic coerce — an invalid value surfaces as a ValidationError rather
            # than an opaque ValueError, mirroring E4's agent.py from_record idiom.
            kind=envelope.get("kind") or "standard",
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
            # Cedar policy engine (Epic 8) — tolerant of pre-E8 envelopes: ids hydrate as
            # None and cedar_enforcement_mode as "none", so ENVELOPE_SCHEMA_VERSION stays 1.
            cedar_policy_engine_id=envelope.get("cedar_policy_engine_id"),
            cedar_policy_engine_arn=envelope.get("cedar_policy_engine_arn"),
            cedar_enforcement_mode=envelope.get("cedar_enforcement_mode") or "none",
            # Multi-tenancy (Epic 24) — tolerant of pre-E24 envelopes that lack these
            # keys: tenant_id hydrates as None, published/shared as False, so
            # ENVELOPE_SCHEMA_VERSION stays 1 (purely additive + backward-compatible).
            tenant_id=envelope.get("tenant_id"),
            published=envelope.get("published") or False,
            shared=envelope.get("shared") or False,
            lifecycle_state=lifecycle_for_status(record["status"]),
            entra_app_id=envelope.get("entra_app_id"),
            # Entra identity + gateway/runtime (Epic 7) — tolerant of pre-E7 envelopes that
            # lack these keys: the 6 ids/handles hydrate as None and identity_status as
            # NONE, so ENVELOPE_SCHEMA_VERSION stays 1 (purely additive + backward-compatible).
            entra_sp_id=envelope.get("entra_sp_id"),
            entra_app_audience=envelope.get("entra_app_audience"),
            invoker_role_id=envelope.get("invoker_role_id"),
            admin_role_id=envelope.get("admin_role_id"),
            gateway_id=envelope.get("gateway_id"),
            gateway_url=envelope.get("gateway_url"),
            # Coerced, NOT validated: an unknown stored value becomes NONE + a warning
            # rather than a ValidationError, because the buildspec writes this key with
            # `jq`. See `coerce_identity_status` in `models.agent`.
            identity_status=coerce_identity_status(envelope.get("identity_status")),
            # Marketplace publication (Epic 33 Amendment 1) — tolerant of pre-E33 envelopes:
            # an absent key (or a stored null) hydrates as None, so ENVELOPE_SCHEMA_VERSION
            # stays 1. A PRESENT but structurally invalid block raises pydantic's
            # ValidationError here; the service's hydrate seam translates that into
            # MalformedMcpRecordError so ONE bad record cannot 500 the whole list view.
            marketplace=(
                MarketplacePublication.model_validate(envelope["marketplace"])
                if envelope.get("marketplace")
                else None
            ),
            created_at=record["createdAt"],
            updated_at=record["updatedAt"],
            created_by=envelope.get("created_by"),
        )
