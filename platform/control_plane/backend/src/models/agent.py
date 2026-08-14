"""Agent Registry domain models + lifecycle/status mapping (Epic 4, Task 1).

The "Agent Registry" is backed by **AWS Agent Registry** (Amazon Bedrock
AgentCore, Preview): one Custom record per agent. The native record carries
`name`/`description`/`status`/`createdAt`/`updatedAt`; our governance metadata is
serialized into the record's `descriptors.custom.data` JSON — the
"envelope" (research §4). `lifecycle_state` is *derived* from the native `status`
(research §5) and is never stored in the envelope.

Mirrors the Base/Create/Update/read split + enum style of `models.operating_model`.
"""

from __future__ import annotations

import logging
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, Field

# The marketplace publication block carried on the envelope (Epic 33). Safe in either
# direction: `models.marketplace` imports nothing from this module.
from models.marketplace import MarketplacePublication

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums (string-valued — JSON-safe via .value)
# ---------------------------------------------------------------------------

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


class DataClassification(str, Enum):
    PUBLIC = "Public"
    INTERNAL = "Internal"
    CONFIDENTIAL = "Confidential"
    RESTRICTED = "Restricted"


class Origin(str, Enum):
    DEPLOYED = "Deployed"
    REGISTERED = "Registered"


class AuthType(str, Enum):
    """How a caller authenticates to invoke the agent (Epic 4b)."""

    NONE = "none"
    ENTRA = "entra"
    API_KEY = "api_key"


class IdentityStatus(str, Enum):
    """Where a record stands in Entra identity provisioning (Epic 36/T20, item 16).

    THE operational gate: `provisioned` is what invoke and every grant route check.
    The frontend mirrors this union verbatim as a CLOSED string-literal type on the two
    full record shapes that carry a provisioning lifecycle — `interface Agent` and
    `interface McpServer`, both in `frontend/src/api/client.ts` — which is what makes the
    frontend's `=== 'provisioned'` gates typo-proof. (Interface NAMES, not line numbers:
    those citations drifted twice in one epic; grep the name.)

    A THIRD frontend spelling is deliberately left OPEN and is NOT a drifted mirror:
    `EligibleAgent.identity_status` (same file) is `string | null`. That is the
    lightweight projection `GET /marketplace/eligible-agents` returns for the MCP subscribe
    picker, where eligibility is already SERVER-enforced, so no frontend consumer compares
    the field against a member (grep: it has none). Closing that union would buy no safety
    and would make the picker's type a second place to edit whenever this vocabulary grows.
    Only the two closed unions above must be updated in lockstep with the members below.

    A `str, Enum` deliberately, because ~10 gates compare it to a bare string
    (`api/routes/grants.py`, `api/routes/mcp_server_grants.py`,
    `services/agent_user_grant.py`, `services/marketplace_service.py`,
    `services/governance_graph_service.py`), which keeps them working unchanged. All 10
    provisioning writers assign a MEMBER: pydantic does not validate assignment, so a
    bare string would bypass this vocabulary AND make pydantic's serializer warn
    (`Expected 'enum' but got 'str'`) on every response that carries the record.
    """

    NONE = "none"
    PENDING = "pending"
    PROVISIONED = "provisioned"
    FAILED = "failed"


def coerce_identity_status(raw) -> "IdentityStatus":
    """Total coercion of a STORED `identity_status` into `IdentityStatus`.

    This DELIBERATELY DIFFERS from the lifecycle-state convention right below
    (`lifecycle_for_status`, which raises `UnknownRegistryStatusError` rather than
    silently defaulting). `identity_status` has an EXTERNAL writer: the CodeBuild
    buildspec patches `identity_status="provisioned"` straight into the stored
    envelope with `jq`
    (`platform/control_plane/infrastructure/modules/codebuild/buildspec.yml:525`),
    so a read must tolerate a foreign value — raising here would let ONE bad record
    500 a whole list route.

    An unknown value coerces to `NONE` + a warning because that FAILS CLOSED: `none`
    loses every `== "provisioned"` gate, so the worst case is an agent that reads as
    un-invokable, never one that reads as authorized. Absent/empty stays `NONE` too,
    preserving the old `envelope.get("identity_status") or "none"` behaviour.
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


# ---------------------------------------------------------------------------
# Lifecycle ↔ registry status mapping (module-level, importable by the service)
# ---------------------------------------------------------------------------

# native registry status (research §5) -> our LifecycleState. Total over the full
# native enum. Transient sync states (CREATING/UPDATING/*_FAILED) collapse onto the
# nearest stable lifecycle: pre-live -> PROPOSED, post-live -> APPROVED.
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

# the target registry status an admin transition sets, keyed by our action verb
TRANSITION_TO_STATUS: dict[str, str] = {
    "approve": "APPROVED",
    "reject": "REJECTED",
    "deprecate": "DEPRECATED",
}


class UnknownRegistryStatusError(ValueError):
    """Raised when a registry record's status is not in STATUS_TO_LIFECYCLE.

    AWS Agent Registry is a Preview service; a future/unknown status value (or
    casing drift) should fail loudly with context rather than as an opaque KeyError
    deep inside record hydration.
    """


def lifecycle_for_status(status: str) -> "LifecycleState":
    """Map a native registry status to our LifecycleState, raising a clear domain
    error on an unmapped value (instead of a bare KeyError)."""
    try:
        return STATUS_TO_LIFECYCLE[status]
    except KeyError:
        raise UnknownRegistryStatusError(
            f"Unmapped AWS Agent Registry status {status!r}; "
            f"known statuses: {sorted(STATUS_TO_LIFECYCLE)}"
        ) from None

# the envelope schema version (research §4); bump if the envelope shape changes
ENVELOPE_SCHEMA_VERSION = 1

# The stage name a runtime carries when the record cannot attribute it to one (E28A/T1).
# Used for a LEGACY scalar-only record's single entry in ``Agent.runtime_arns`` and as
# ``RuntimeStatus.stage`` for that same record — one value, so the "we do not know" answer
# reads identically wherever it surfaces. Also the value ``runtime_status`` reported
# unconditionally before the map existed, which keeps the frontend's `runtimeScope` behaviour
# on a legacy record byte-identical: it treats a non-`unknown` stage as attributable.
UNKNOWN_STAGE = "unknown"


def resolve_runtime_arns(agent) -> Dict[str, str]:
    """Every runtime an agent owns, as ``stage -> ARN`` (E28A/T1, contract C-A2).

    THE SINGLE PLACE the map-else-scalar resolution lives, so no caller has to decide for
    itself which of the two fields is authoritative — three of the four D-A4 defects exist
    precisely because each call site made that choice independently.

    - A POPULATED ``agent_arns`` wins outright: it is the complete inventory, and C-A2 has the
      buildspec write the scalar as a duplicate of the LAST stage's ARN, so unioning the
      scalar in would merely re-list one runtime under a second key — and then the E23
      cascade would call ``DeleteAgentRuntime`` on it twice.
    - An ABSENT/EMPTY map with a scalar present is a LEGACY RECORD (pre-E28A, or an agent
      whose next deploy has not yet run under T1b's buildspec). It yields exactly ONE entry,
      keyed :data:`UNKNOWN_STAGE`, because the record genuinely does not know which stage that
      runtime belongs to — naming it ``"dev"`` would be a fabrication, and ``runtime_status``
      reports the key straight through as its ``stage``.
    - Neither ⇒ ``{}``. Nothing was ever provisioned (the E20 pre-registration state).

    Callers that must act on N runtimes iterate ``.values()``; callers that need a stage for
    one runtime read the keys. Every one of them still derives the control-plane
    ``agentRuntimeId`` exactly as before (``arn.rsplit("/", 1)[-1]``) — this changes WHICH ARNs
    a path touches, never how an ARN resolves to a runtime.

    Takes a DUCK-TYPED agent, not an :class:`Agent`, because the delete cascade reads a record
    that may be any object carrying the two fields, and because ``getattr`` defaults are what
    make a legacy/partial record a non-error here rather than an ``AttributeError``.
    """
    mapped = getattr(agent, "agent_arns", None) or {}
    if mapped:
        return dict(mapped)
    scalar = getattr(agent, "agent_arn", None)
    if scalar:
        return {UNKNOWN_STAGE: scalar}
    return {}


# ---------------------------------------------------------------------------
# Models (mirror operating_model's Base/Create/Update/read split)
# ---------------------------------------------------------------------------

class AgentBase(BaseModel):
    # `model_id` collides with pydantic's protected `model_` namespace — it's a plain
    # data field (the Bedrock model), not a pydantic internal, so opt the family out.
    model_config = {"protected_namespaces": ()}

    name: str = Field(..., min_length=1, max_length=255)
    purpose: Optional[str] = Field(default="", max_length=4096)
    sponsor_oid: Optional[str] = None
    sponsor_email: Optional[str] = None
    business_unit: Optional[str] = None
    region: Optional[str] = None
    data_classification: Optional[DataClassification] = None
    platform: Optional[Platform] = None
    framework: Optional[str] = None
    # the Bedrock model the runtime container invokes (agent.config.json → the buildspec's
    # runtime tfvars). Additive; pre-E21 envelopes lack this key.
    model_id: Optional[str] = None
    mcp_server_ids: List[str] = Field(default_factory=list)
    origin: Origin = Origin.REGISTERED
    # invocation info (Epic 4b) — how callers reach/authenticate to the agent
    endpoint_url: Optional[str] = None
    auth_type: AuthType = AuthType.NONE
    agent_arn: Optional[str] = None
    # Per-stage runtime ARNs (Epic 28A/T1, contract C-A2) — stage name -> runtime ARN.
    #
    # WHY BOTH THIS AND `agent_arn`. E28A/T1b stage-scopes the runtime module's RESOURCE
    # names, so an agent genuinely owns one runtime PER STAGE and a single scalar can name
    # only one of them (D-A4: that single scalar is the root of four real defects — the E23
    # delete leaking N-1 runtimes, `/invoke` reaching whichever stage deployed last, the
    # Entra JWT authorizer wired on one runtime while the other is born UNAUTHORIZED, and
    # grant-time MCP env injection landing on one runtime only).
    #
    # `agent_arn` KEEPS its current meaning ("whichever stage deployed last") rather than
    # being replaced, and C-A2 has the buildspec write BOTH: a rollback to pre-E28A code
    # still finds a runtime through the scalar, and every pre-E28A envelope in the registry
    # today has no map at all. So an ABSENT/EMPTY map is a LEGACY RECORD, NOT an error —
    # readers iterate the map when populated and fall back to the scalar when it is not.
    # See :meth:`Agent.runtime_arns` — the ONE place that fallback is expressed.
    agent_arns: Dict[str, str] = Field(default_factory=dict)
    # Multi-tenancy (Epic 24) — the owning tenant + cross-tenant publish flag.
    # Optional on Base (pre-E24 records hydrate as None); REQUIRED on AgentCreate.
    tenant_id: Optional[str] = None
    published: bool = False
    # Databricks-hosted runtimes (Epic 29/T5, contract C-4) — the second governed
    # runtime platform. All additive + optional: every record that exists today (and
    # every AgentCore record ever) carries None for all six, which is exactly
    # "not Databricks-governed". ENVELOPE_SCHEMA_VERSION stays 1.
    #
    # WHY ON AgentBase AND NOT ON `Agent` ONLY (unlike the E6 identity ids). The first
    # three arrive AT REGISTRATION, from the caller: discovery (T3) lists the tenant's
    # apps/endpoints and the wizard (T8) posts the one the operator picked. They are
    # descriptive facts about where the agent already runs — the same category as
    # `agent_arn`, which is likewise caller-supplied on Base. The last three are
    # SERVICE-written by T6's provisioning and simply ride the same family so one
    # envelope round-trip covers all six; nothing user-supplied is trusted by the gate
    # beyond "there is a runtime here", and `is_databricks_governed` still requires
    # `auth_type == ENTRA` before any identity work happens.
    #
    # `agent_arn` stays AgentCore-ONLY and is NOT reused as a generic handle: the delete
    # cascade, the runtime-status probe, and the per-stage `agent_arns` map all parse it
    # as a Bedrock ARN, so putting a Databricks URL in it would silently feed a URL to
    # `arn.rsplit("/", 1)[-1]`. Two fields, two platforms, no overloading.
    runtime_handle: Optional[str] = None            # Databricks app URL (or serving-endpoint handle)
    runtime_kind: Optional[str] = None              # "app" | "serving_endpoint"
    # "" (aws) | "federation" | "invoke_unavailable" | "sp_secret" (dormant, §3B) — copied from
    # the tenant at register. Plain ``str``: a stored envelope may carry any of them.
    binding_mode: Optional[str] = None
    databricks_sp_id: Optional[str] = None          # sp_secret mode: the per-agent SP application_id
    # The Secrets Manager ARN of the per-agent SP secret — the ARN ONLY. The secret VALUE
    # lives exclusively in Secrets Manager and never touches this model, the envelope, a
    # response, or a log (Global Constraints: secrets discipline).
    databricks_sp_secret_arn: Optional[str] = None
    oauth2_app_client_id: Optional[str] = None      # the app's Databricks OAuth client id (federation mode)


class AgentCreate(AgentBase):
    """Create payload. `tenant_id` is REQUIRED on new agents (E24 spec §6);
    sponsor stays optional -> 'Ownerless'.

    Deliberately carries NO `project_id` (E27/T5) — see the field's comment on `Agent`.
    A body key of that name is dropped by pydantic's default `extra="ignore"`, exactly
    like `AgentUpdate` already drops it.
    """

    tenant_id: str


class AgentUpdate(BaseModel):
    """Standalone — every field Optional (drives model_dump(exclude_none=True))."""

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
    origin: Optional[Origin] = None
    # invocation info (Epic 4b)
    endpoint_url: Optional[str] = None
    auth_type: Optional[AuthType] = None
    # Multi-tenancy (Epic 24/T5) — the publish route flips this via the existing
    # read-modify-write update path; absent (None) means "leave unchanged".
    published: Optional[bool] = None
    agent_arn: Optional[str] = None
    # DELIBERATELY NO `agent_arns` (E28A/T1). The map's only writers are the buildspec (which
    # patches the envelope JSON directly) and ``persist_identity`` (which envelope-writes an
    # in-hand ``Agent``) — no route needs to PATCH it, and ``update()``'s read-modify-write
    # already PRESERVES a field absent from the payload, so omitting it here cannot lose one.
    # Do not "fix" the asymmetry with ``agent_arn`` by adding it: a body key that could
    # re-point an agent's per-stage runtimes is a wider surface than the map needs.


class Agent(AgentBase):
    id: str  # the registry recordId — assigned by the registry (no default_factory)
    lifecycle_state: LifecycleState  # derived from the native status, set by the service
    entra_app_id: Optional[str] = None
    entra_api_app_id: Optional[str] = None
    # Entra identity provisioning (Epic 6) — SERVICE-WRITTEN (like entra_app_id), set by
    # the T-IDENTITY provisioning hook, never user-supplied; so on Agent + envelope only,
    # NOT AgentBase/Create/Update. Research §4.
    entra_sp_id: Optional[str] = None          # agent SP object id — for appRoleAssignedTo / oauth2PermissionGrants
    # entra_app_audience: the per-agent token aud == AgentCore allowedAudience. Per-agent
    # apps set requestedAccessTokenVersion=2, so the OBO'd token aud is the
    # api://agp-agent-<id> URI — which T-IDENTITY persists here. (The spike's GUID-form aud
    # was the BACKEND app, a different app/token-version, and does not govern these.) The
    # live runbook keeps a one-time decode of an OBO'd per-agent token to CONFIRM the aud
    # form — a verification check, not a hard requirement; if it ever comes back as the
    # client GUID, switch to the app's client-id GUID. See research §1 AUDIENCE-FORM FINDING.
    entra_app_audience: Optional[str] = None
    invoker_role_id: Optional[str] = None      # appRole GUID minted at provisioning
    admin_role_id: Optional[str] = None        # appRole GUID minted at provisioning
    # The operational gate — a PINNED enum since E36/T20 (was a free `str`). Still
    # tolerant on the READ side via `coerce_identity_status` in `from_record`, because
    # the buildspec writes this key into the envelope externally.
    identity_status: IdentityStatus = IdentityStatus.NONE
    # AgentCore OAuth2 credential-provider name (Epic 7, Tier-2, T-CRED-PROVIDER) —
    # SERVICE-WRITTEN (like the E6 identity ids): the NAME of the per-agent
    # MicrosoftOauth2 credential provider in AgentCore Identity's Token Vault, set by
    # agent_credential_service.ensure_agent_credential_provider. A NON-secret (the
    # client secret itself stays vaulted, never persisted here). The reference agent
    # reads this name as its CREDENTIAL_PROVIDER_NAME env var (research §3.5). Additive,
    # backward-compatible: ENVELOPE_SCHEMA_VERSION stays 1.
    oauth2_credential_provider_name: Optional[str] = None
    # Per-project ownership (Epic 27/T5) — the project whose roles gate this agent's
    # mutation routes. SERVICE-WRITTEN (like the E6 identity ids above): stamped by
    # ProjectService via the `project_id` keyword on ``AgentRegistryService.create`` at
    # repo-materialize pre-register time, so it lives on `Agent` + the envelope ONLY —
    # NOT on AgentBase/Create/Update. An agent must never be parented (or re-parented)
    # into a project by a REQUEST BODY, which would let a caller plant a record into, or
    # move one under, a project they happen to own. ADDITIVE and optional exactly like
    # tenant_id: an agent registered directly (or any pre-E27 record) has None and stays
    # tenant-gated only.
    project_id: Optional[str] = None

    # Langfuse observability (Epic 26) — SERVICE-WRITTEN join fields set by
    # LangfuseProvisioningService.provision_agent_project at register time (like the E6
    # identity ids): the platform auto-provisions a Langfuse project + key PER AGENT so
    # traces land in the agent's own project (structural attribution — no trace tags).
    # Only the secret NAME + the project id live here; the key VALUES (public/secret key)
    # stay in Secrets Manager and NEVER touch the envelope, a response, or a log. Additive,
    # backward-compatible: ENVELOPE_SCHEMA_VERSION stays 1.
    langfuse_project_id: Optional[str] = None          # Langfuse project id, e.g. "clx…"; None = not provisioned
    langfuse_key_secret_name: Optional[str] = None     # Secrets Manager name holding {public_key, secret_key}

    # Marketplace publication (Epic 33) — the approved DECLARED datasheet plus its
    # attestation (who declared it, when). SERVICE-WRITTEN, for exactly the reason
    # `project_id` above is (see its comment): a marketplace datasheet is an ATTESTATION, so
    # it must never be settable by a REQUEST BODY — that would let a publisher self-certify
    # an "admin-approved" SLA tier and compliance list. It therefore lives on `Agent` + the
    # envelope ONLY (NOT on AgentBase/AgentCreate/AgentUpdate) and is persisted through the
    # dedicated ``AgentRegistryService.persist_marketplace``, which the approve path is the
    # only caller of. None = never published (and every pre-E33 record).
    #
    # NOT the E24 `published` flag above: that is the cross-TENANT visibility flag flipped by
    # `PUT /agents/{id}/publish`. Marketplace publication is a different feature with a
    # different approver, which is why every name here carries the `marketplace` prefix.
    #
    # No import cycle: `models.marketplace` imports nothing from `models.agent`.
    marketplace: Optional[MarketplacePublication] = None
    created_at: datetime
    updated_at: datetime
    created_by: Optional[str] = None

    @property
    def is_agentcore(self) -> bool:
        """True for a manually-created AgentCore Runtime agent (arn + Entra + Bedrock):
        the provisioning gate (research §0, Decision 5). Single source of truth for both
        ``agent_registry_service.create()`` and ``agent_identity_service.provision()``."""
        return (
            bool(self.agent_arn)
            and self.auth_type == AuthType.ENTRA
            and self.platform == Platform.AWS_BEDROCK
        )

    @property
    def is_databricks_governed(self) -> bool:
        """True for a Databricks-hosted agent AGP governs (handle + Entra + Databricks):
        the second platform's provisioning/invoke gate (E29/T5, contract C-4).

        The exact structural mirror of :attr:`is_agentcore` — a runtime we can reach, an
        identity model we own, and the platform this branch knows how to talk to — so the
        two gates read the same and, because they demand different ``platform`` values, can
        never both be True. That mutual exclusivity is what makes the create hook's
        ``if is_agentcore / elif is_databricks_governed`` a dispatch rather than a race.

        ``runtime_handle`` is the "is there actually something running?" leg (``agent_arn``
        plays it for AgentCore): a Databricks record without one is inert metadata, exactly
        like the ~18 metadata-only seed agents. ``auth_type == ENTRA`` keeps this an
        IDENTITY gate — an api_key agent gets no Entra app, no OBO, no grants.

        DELIBERATELY does NOT look at ``binding_mode``. Which credential path provisioning
        takes (federation vs sp_secret) is T6's business; whether the agent is governed at
        all must not depend on a field that is copied from the tenant and can be absent on
        a record written before its mode was resolved.
        """
        return (
            bool(self.runtime_handle)
            and self.auth_type == AuthType.ENTRA
            and self.platform == Platform.DATABRICKS
        )

    def runtime_arns(self) -> Dict[str, str]:
        """Every runtime this agent owns, as ``stage -> ARN`` (E28A/T1, contract C-A2).

        Thin wrapper that delegates to :func:`resolve_runtime_arns` — the same
        model-property-delegates-to-module-function split ``is_agentcore`` /
        ``is_agentcore_agent`` already uses, and for the same reason: one implementation,
        two ergonomic call shapes.
        """
        return resolve_runtime_arns(self)

    # -- envelope (de)serialization -----------------------------------------

    def to_envelope(self) -> dict:
        """Return the research-§4 governance JSON dict for the Custom record.

        INCLUDES `schema_version` + the governance fields. EXCLUDES the native
        record fields (`name`, `purpose`→record `description`, `created_at`,
        `updated_at`) and the derived `lifecycle_state` (never stored). Enums are
        serialized as their `.value` so the dict is JSON-safe (json.dumps-able).
        """
        return {
            "schema_version": ENVELOPE_SCHEMA_VERSION,
            "agent_id": self.id,  # mirror of recordId for debug convenience
            "sponsor_oid": self.sponsor_oid,
            "sponsor_email": self.sponsor_email,
            "business_unit": self.business_unit,
            "region": self.region,
            "data_classification": self.data_classification.value if self.data_classification else None,
            "platform": self.platform.value if self.platform else None,
            "framework": self.framework,
            # model_id (Epic 21) — additive; pre-E21 envelopes lack this key.
            "model_id": self.model_id,
            "mcp_server_ids": list(self.mcp_server_ids),
            "entra_app_id": self.entra_app_id,
            "entra_api_app_id": self.entra_api_app_id,
            "origin": self.origin.value if isinstance(self.origin, Origin) else self.origin,
            "created_by": self.created_by,
            # invocation info (Epic 4b) — additive; old envelopes lack these keys
            "endpoint_url": self.endpoint_url,
            "auth_type": self.auth_type.value if isinstance(self.auth_type, AuthType) else self.auth_type,
            "agent_arn": self.agent_arn,
            # Per-stage runtime ARNs (Epic 28A/T1, C-A2) — additive; pre-E28A envelopes lack
            # this key. THIS WRITE IS THE ORDERING CONSTRAINT the plan names: the buildspec
            # patches the stored envelope JSON directly, but every subsequent
            # ``persist_identity``/``update`` re-serializes the envelope from THIS dict — so
            # dropping the key here would silently discard a map the buildspec had just
            # written, on the very next identity persist. Written even when empty (a plain
            # ``{}``), exactly like the other additive keys are written as ``None``.
            "agent_arns": dict(self.agent_arns),
            # Entra identity provisioning (Epic 6) — additive; pre-E6 envelopes lack these keys
            "entra_sp_id": self.entra_sp_id,
            "entra_app_audience": self.entra_app_audience,
            "invoker_role_id": self.invoker_role_id,
            "admin_role_id": self.admin_role_id,
            # `.value` — the envelope is json.dumps-ed into storage, so a stored enum
            # repr ("IdentityStatus.NONE") would be a data-corruption bug. The
            # isinstance guard mirrors `origin`/`auth_type` above and stays defensive:
            # the live writers all assign a member, but pydantic does not validate
            # assignment, so a raw string must serialize through, not AttributeError.
            "identity_status": (
                self.identity_status.value
                if isinstance(self.identity_status, IdentityStatus)
                else self.identity_status
            ),
            # AgentCore credential-provider name (Epic 7, Tier-2) — additive; pre-E7
            # envelopes lack this key. A NON-secret (the vaulted client secret is never
            # serialized here).
            "oauth2_credential_provider_name": self.oauth2_credential_provider_name,
            # Multi-tenancy (Epic 24) — additive; pre-E24 envelopes lack these keys.
            "tenant_id": self.tenant_id,
            "published": self.published,
            # Per-project ownership (Epic 27/T5) — additive; pre-E27 envelopes lack this key.
            "project_id": self.project_id,
            # Langfuse observability (Epic 26) — additive; pre-E26 envelopes lack these
            # keys. The secret NAME + project id ONLY; the key VALUES never touch here.
            "langfuse_project_id": self.langfuse_project_id,
            "langfuse_key_secret_name": self.langfuse_key_secret_name,
            # Databricks runtimes (Epic 29/T5, C-4) — additive; pre-E29 envelopes lack
            # these keys. Written unconditionally (as ``None`` when unset) like every
            # additive round before this one, so a later identity persist cannot drop a
            # handle that registration had just written. `databricks_sp_secret_arn` is an
            # ARN, never the secret value.
            "runtime_handle": self.runtime_handle,
            "runtime_kind": self.runtime_kind,
            "binding_mode": self.binding_mode,
            "databricks_sp_id": self.databricks_sp_id,
            "databricks_sp_secret_arn": self.databricks_sp_secret_arn,
            "oauth2_app_client_id": self.oauth2_app_client_id,
            # Marketplace publication (Epic 33) — additive; pre-E33 envelopes lack this key.
            # ``mode="json"`` so the nested ``declared_at`` datetime becomes an ISO string and
            # the whole envelope stays json.dumps-able. Written as an explicit ``null`` when
            # absent, like every other additive key: OMITTING it would leave a previously
            # stored block in place forever, so an unpublish-to-nothing could never land.
            "marketplace": self.marketplace.model_dump(mode="json") if self.marketplace else None,
        }

    @classmethod
    def from_record(cls, record: dict, envelope: dict) -> "Agent":
        """Build an Agent from a GetRegistryRecord-style response + parsed envelope.

        `record` keys: recordId, name, displayName, description, status, createdAt,
        updatedAt, recordType. `envelope` is the parsed `descriptors.custom.data`.
        Native fields (name/description/createdAt/updatedAt/recordId/status) are
        authoritative; everything else comes from the envelope. Enum values in the
        envelope may be plain strings (from JSON) — Pydantic coerces them.

        E32 added the native `displayName`, which is what the registry now shows and what
        our `create` writes, so it is preferred here. `name` remains the fall-back so
        records written before the migration (which have no `displayName`) still hydrate
        rather than KeyError-ing the whole list view.
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
            # model_id (Epic 21) — tolerant of pre-E21 envelopes that lack the key.
            model_id=envelope.get("model_id"),
            mcp_server_ids=envelope.get("mcp_server_ids") or [],
            entra_app_id=envelope.get("entra_app_id"),
            entra_api_app_id=envelope.get("entra_api_app_id"),
            origin=envelope.get("origin") or Origin.REGISTERED,
            created_by=envelope.get("created_by"),
            # invocation info (Epic 4b) — tolerant of pre-4b envelopes that lack
            # these keys: they hydrate with defaults (auth_type=NONE, others None),
            # so ENVELOPE_SCHEMA_VERSION stays 1 (purely additive + backward-compatible).
            endpoint_url=envelope.get("endpoint_url"),
            auth_type=envelope.get("auth_type") or AuthType.NONE,
            agent_arn=envelope.get("agent_arn"),
            # Per-stage runtime ARNs (Epic 28A/T1, C-A2) — tolerant of a pre-E28A envelope
            # that lacks the key: it hydrates as ``{}``, which ``runtime_arns`` reads as
            # "legacy record, use the scalar". A missing map is NEVER an error. ``or {}``
            # also absorbs a stored explicit ``null``, which `jq` can leave behind.
            agent_arns=envelope.get("agent_arns") or {},
            # Entra identity provisioning (Epic 6) — tolerant of pre-E6 envelopes that lack
            # these keys: the 4 ids hydrate as None and identity_status as NONE, so
            # ENVELOPE_SCHEMA_VERSION stays 1 (purely additive + backward-compatible).
            entra_sp_id=envelope.get("entra_sp_id"),
            entra_app_audience=envelope.get("entra_app_audience"),
            invoker_role_id=envelope.get("invoker_role_id"),
            admin_role_id=envelope.get("admin_role_id"),
            # Coerced, NOT validated: an unknown stored value becomes NONE + a warning
            # rather than a ValidationError, because the buildspec writes this key with
            # `jq`. See `coerce_identity_status`.
            identity_status=coerce_identity_status(envelope.get("identity_status")),
            # AgentCore credential-provider name (Epic 7, Tier-2) — tolerant of pre-E7
            # envelopes that lack the key: hydrates as None (additive + backward-compatible).
            oauth2_credential_provider_name=envelope.get("oauth2_credential_provider_name"),
            # Multi-tenancy (Epic 24) — tolerant of pre-E24 envelopes that lack these
            # keys: tenant_id hydrates as None and published as False, so
            # ENVELOPE_SCHEMA_VERSION stays 1 (purely additive + backward-compatible).
            tenant_id=envelope.get("tenant_id"),
            published=envelope.get("published") or False,
            # Per-project ownership (Epic 27/T5) — tolerant of pre-E27 envelopes that lack
            # the key: hydrates as None, which is exactly "not project-governed" and keeps
            # the agent tenant-gated only (additive + backward-compatible).
            project_id=envelope.get("project_id"),
            # Langfuse observability (Epic 26) — tolerant of pre-E26 envelopes that lack
            # these keys: both hydrate as None (additive + backward-compatible).
            langfuse_project_id=envelope.get("langfuse_project_id"),
            langfuse_key_secret_name=envelope.get("langfuse_key_secret_name"),
            # Databricks runtimes (Epic 29/T5, C-4) — tolerant of a pre-E29 envelope that
            # lacks all six keys: they hydrate as None, which reads as "not
            # Databricks-governed" and leaves the record exactly as it behaves today. That
            # is EVERY record currently in the registry, so a missing key is never an error
            # (additive + backward-compatible; ENVELOPE_SCHEMA_VERSION stays 1).
            runtime_handle=envelope.get("runtime_handle"),
            runtime_kind=envelope.get("runtime_kind"),
            binding_mode=envelope.get("binding_mode"),
            databricks_sp_id=envelope.get("databricks_sp_id"),
            databricks_sp_secret_arn=envelope.get("databricks_sp_secret_arn"),
            oauth2_app_client_id=envelope.get("oauth2_app_client_id"),
            # Marketplace publication (Epic 33) — tolerant of pre-E33 envelopes that lack the
            # key AND of a stored explicit ``null``: both hydrate as None, which is exactly
            # "never published" (additive + backward-compatible, so
            # ENVELOPE_SCHEMA_VERSION stays 1).
            marketplace=(
                MarketplacePublication.model_validate(envelope["marketplace"])
                if envelope.get("marketplace")
                else None
            ),
        )


def is_databricks_governed_agent(agent: Agent) -> bool:
    """The Databricks provisioning/invoke gate (E29/T5, contract C-4).

    Thin wrapper that delegates to :attr:`Agent.is_databricks_governed` — the same
    model-property-delegates-to-module-function split ``is_agentcore`` /
    ``is_agentcore_agent`` and ``runtime_arns`` / ``resolve_runtime_arns`` already use, so
    the boolean logic lives in exactly one place and callers get two ergonomic call shapes.

    Lives HERE (beside the model) rather than in a service, because unlike
    ``is_agentcore_agent`` — which sits in ``agent_identity_service`` for historical
    reasons — this gate has no service to belong to: the Databricks provisioning branch is
    dispatched from the route and from ``agent_identity_service.provision``, and putting the
    predicate in either would make the other import across a seam it does not otherwise use.
    """
    return agent.is_databricks_governed


# ---------------------------------------------------------------------------
# Runtime status (Epic 28/T5, design D9, contract C2)
# ---------------------------------------------------------------------------

# The CLOSED status union. Pinned by C2 and mirrored verbatim by the frontend's
# `RUNTIME_STATUSES` (C3) whose `Record<RuntimeStatusKey, …>` tables have NO default
# branch — so adding a 7th value here is a design change that breaks the compiler on the
# other side, not a free addition. `AgentIdentityService.runtime_status` is the only
# producer; it maps every native AgentCore status into one of these.
RUNTIME_STATUSES = (
    "ready",
    "creating",
    "updating",
    "failed",
    "not_deployed",
    "unknown",
)


class RuntimeStatus(BaseModel):
    """A point-in-time read of one agent's AgentCore Runtime (E28/T5, contract C2).

    NOT persisted and NOT part of the governance envelope: this is a live probe result,
    and a stored copy would go stale silently — the product's whole problem was that "is
    this agent up?" had no honest answer. `checked_at` exists so the UI can say WHEN.

    `status` is a plain `str` (not an Enum) because C2 pins it as one and the frontend owns
    the exhaustiveness check; the producer guarantees the value is in `RUNTIME_STATUSES`.

    `"unknown"` is MANDATORY and DISTINCT from `"failed"`: an unreachable/denying control
    plane is not a broken runtime. Conflating them makes a governance product report a
    probe failure as a production outage — the same rule D13 applies to metrics ("not
    instrumented" is never a real zero).

    `stage` is free-form (D8). Since E28A/T1 it names a REAL stage whenever the record's
    `agent_arns` map can attribute the probed runtime to one, and is `UNKNOWN_STAGE` for a
    legacy scalar-only record that genuinely cannot — see
    `AgentIdentityService.runtime_status`. Never guess: the frontend's `runtimeScope` treats a
    named stage as attributable evidence and will caption a per-stage pill with it.

    `detail` is a SAFE short hint ONLY — never a token, a credential, a raw upstream body,
    or anything carrying an AWS account id. `runtime_arn` is one of the agent's own runtime
    ARNs, which every agent route already returns, so it is not a new disclosure; NO other
    field may carry it.
    """

    agent_id: str
    stage: str
    status: str
    runtime_arn: Optional[str] = None
    image_tag: Optional[str] = None
    checked_at: str
    detail: Optional[str] = None
