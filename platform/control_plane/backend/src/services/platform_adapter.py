"""The platform-adapter seam: one object per governed platform, and the typed errors it raises.

``PlatformAdapter`` is a structural protocol with seven verbs. ``services/platform_adapters/`` holds
one implementation per platform and ``services/platform_adapter_factory.py`` builds and dispatches
them, so this module imports no platform's service graph. Every adapter delegates to the service that
owns the behaviour; the wrapper owns dispatch. Writes with no collaborator wired raise
``AdapterError`` (a silent skip strands a record at ``pending`` with no error); reads degrade to
``unknown`` with a fixed hint and never raise (a status read that 5xx'd would blank the fleet view).
``InvokeError`` is the invoke verb's refusal vocabulary on both platforms: its kinds are the codes
support greps for, and its table is what the client sees for each.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, ClassVar, Protocol, runtime_checkable

from core.errors import AppError, Outcome

if TYPE_CHECKING:
    from api.routes.agents import InvokeRequest
    from core.rbac import Principal
    from models.agent import Agent
    from models.tenant import Tenant
    from services.agent_identity_service import RuntimeStatusWithEnv
    from services.platform_registry import PlatformDeclaration

UPSTREAM_ERROR = "agent returned an error response"


class AdapterError(AppError):
    """The seam's own refusal: AGP cannot act on this platform here.

    ``platform_unsupported`` is "no adapter for the platform, or no collaborator wired for it";
    ``artifact_unremovable`` is "a record names a platform artifact nothing on this deployment can
    remove". The message is fixed text from this codebase, never a platform value or an upstream
    body, and every kind has a fixed detail. A collaborator's own failures (``ProvisioningError``,
    ``DatabricksError``) pass through untranslated: they carry the diagnosis. ``kind`` is
    keyword-only so a swapped pair of strings cannot put a sentence where a code belongs.
    """

    domain: ClassVar[str] = "adapter"
    kinds: ClassVar[Mapping[str, Outcome]] = {
        "platform_unsupported": Outcome(502, "the agent could not be invoked [platform_unsupported]"),
        "artifact_unremovable": Outcome(
            500,
            "the agent's Databricks bindings cannot be removed on this deployment, so it was not deleted",
        ),
    }

    def __init__(self, message: str, *, kind: str) -> None:
        super().__init__(message, kind)


class InvokeError(AppError):
    """An invoke was refused or failed, on either platform.

    Every kind but two has a fixed detail, most carrying the code in brackets. ``upstream_error``
    and ``tools_ungranted`` send the message as the detail; the AWS adapter composes it from
    ``UPSTREAM_ERROR``, an exception type that matches ``[A-Za-z]{1,64}``, and MCP server names
    from AGP's own registry, never from a response body, a URL or a token. ``not_provisioned`` and
    ``unknown_stage`` are the invoke route's own refusals, decided before any token exchange.
    """

    domain: ClassVar[str] = "invoke"
    kinds: ClassVar[Mapping[str, Outcome]] = {
        "not_provisioned": Outcome(409, "agent identity is not provisioned"),
        "unknown_stage": Outcome(404, "unknown stage"),
        "token_rejected": Outcome(502, "agent rejected the token"),
        "upstream_error": Outcome(502, None),
        "timed_out": Outcome(504, "agent invocation timed out"),
        "unreachable": Outcome(502, "failed to reach the agent"),
        "arn_malformed": Outcome(502, "agent ARN is malformed"),
        "agent_arn_invalid": Outcome(502, "the agent could not be invoked [agent_arn_invalid]"),
        "tools_ungranted": Outcome(502, None),
        "invalid_runtime_handle": Outcome(502, "the agent could not be invoked [invalid_runtime_handle]"),
        "tenant_unresolved": Outcome(502, "the agent could not be invoked [tenant_unresolved]"),
        "workspace_url_invalid": Outcome(502, "the agent could not be invoked [workspace_url_invalid]"),
        "workspace_stage_unresolved": Outcome(
            502,
            "AGP cannot tell which of this tenant's Databricks workspaces hosts the agent. A "
            "Databricks Apps hostname carries no workspace identity, so a tenant with more than "
            "one workspace stage cannot be disambiguated from the app URL alone — the fix is on "
            "the tenant record: have provisioning record which workspace stage the agent was "
            "bound in [workspace_stage_unresolved]",
        ),
        "sp_credential_unavailable": Outcome(
            502, "the agent could not be invoked [sp_credential_unavailable]"
        ),
        "federation_exchange_failed": Outcome(
            502, "the agent could not be invoked [federation_exchange_failed]"
        ),
        "federation_unavailable": Outcome(
            502,
            "this agent's Databricks tenant cannot federate, so AGP will not invoke it: federation "
            "needs BOTH an account-admin credential on the tenant and this Entra tenant's "
            "identities synced into the Databricks account (user sync). Add them and re-connect the "
            "tenant so its capabilities are re-probed. AGP does not fall back to service-principal "
            "binding, because that would silently change what the audit log can prove "
            "[federation_unavailable]",
        ),
        "sp_secret_disabled": Outcome(
            502,
            "this agent carries the dormant per-agent service-principal Databricks binding, which "
            "this deployment does not allow: every call would be attributed to a service principal "
            "instead of the caller. Set DATABRICKS_ALLOW_SP_SECRET_BINDING=true to enable it "
            "deliberately, or move the tenant to federation (account-admin credential + user sync) "
            "[sp_secret_disabled]",
        ),
        "token_mint_failed": Outcome(502, "the agent could not be invoked [token_mint_failed]"),
        "binding_mode_unresolved": Outcome(502, "the agent could not be invoked [binding_mode_unresolved]"),
    }


class ArtifactPolicy(str, Enum):
    """How teardown treats one artifact a record names when the record is deleted.

    ``BLOCKING``: a removal that fails aborts the delete and leaves the record, because the record
    is the only thing that still names which resource to withdraw (the Databricks federation
    audience, trust state on the customer's account). ``BEST_EFFORT``: a removal that fails is
    logged as named residue and the delete proceeds; the resource is ours, in our account.
    ``NEVER_TOUCH``: reserved for a tenant-shared trust object; skipped with a reason, and no
    artifact carries it today.
    """

    BLOCKING = "blocking"
    BEST_EFFORT = "best_effort"
    NEVER_TOUCH = "never_touch"


@dataclass(frozen=True)
class Artifact:
    """One thing a record names that outlives it unless teardown removes it.

    ``kind`` is a fixed literal this codebase wrote (``federation_audience``, ``agent_secret``,
    ``platform_sp``, ...), used to dispatch the removal and to name the residue in a log; ``ref``
    is a safe identifier for that log line and carries no secret and no account id. Frozen so an
    enumerated artifact cannot change between the enumeration and its removal.
    """

    kind: str
    ref: str
    policy: ArtifactPolicy


@runtime_checkable
class PlatformAdapter(Protocol):
    """What AGP does to one platform. Structural: an implementation neither inherits from nor
    registers with this, and ``test_platform_adapter`` compares the signatures below parameter
    by parameter against both adapters."""

    def declaration(self) -> PlatformDeclaration:
        """This platform's declaration, from ``services.platform_registry.DECLARATIONS``; never
        built here, so an adapter cannot hold a mutable copy of the support matrix."""
        ...

    async def probe(self, tenant: Tenant, admin_creds: tuple[str, str] | None = None) -> tuple[dict, str]:
        """Gather evidence about one tenant's grant: ``(capabilities, binding_mode)``, the pair
        ``tenant_service`` persists. ``admin_creds`` carries create-time credentials that exist
        only in memory; ``None`` means "resolve the stored credential yourself". Fails closed and
        never invents a capability: the result is persisted and read as a fact about the tenant."""
        ...

    async def provision_runtime_binding(self, agent: Agent) -> None:
        """Install the platform-side binding that makes the agent's runtime governed. A write: it
        raises rather than no-ops, and returns ``None`` because the registry already owns the
        persisted record. AgentCore has no platform-side ACL, so AGP wires the runtime's inbound
        JWT authorizer; Databricks enforces its own permissions, so AGP mirrors the grant into the
        workspace's ACL."""
        ...

    def runtime_status(self, agent: Agent, stage: str | None = None) -> RuntimeStatusWithEnv:
        """One agent's live runtime status. Never raises; sync on both platforms (the route runs it
        through ``anyio.to_thread.run_sync``). ``stage is None`` means whichever runtime the record
        names, and a record with no runtime anywhere reads ``not_deployed`` without touching the
        platform. ``environment_variables`` is ``None`` when the read holds no evidence about the
        env and ``{}`` when the env was read and is empty."""
        ...

    async def invoke(
        self,
        agent: Agent,
        req: InvokeRequest,
        principal: Principal,
        obo_token: str,
        stage: str | None = None,
        actor_id: str | None = None,
    ) -> dict:
        """Send one prompt to the agent's runtime and return ``{"response": ..., "session_id": str |
        None, "tool_calls": list[{"name", "args"}] | None}``, the same dict on both platforms.
        ``obo_token`` arrives already exchanged: the route runs the on-behalf-of exchange before
        dispatching, so the caller's grant is checked before any platform is contacted. Refusals
        and failures raise ``InvokeError``; the seam's own refusal (no adapter for the platform) is
        ``AdapterError``. ``stage`` selects the runtime (the route has already refused a stage the
        agent owns no runtime for); ``actor_id`` overrides an AWS harness's memory actor. Platforms
        for which either means nothing accept and ignore it."""
        ...

    def named_artifacts(self, agent: Agent) -> list[Artifact]:
        """Everything this record names that teardown must handle. A sync read over the record's
        own fields that never raises and needs no collaborator, so ``DELETE /agents/{id}`` can
        decide from the enumeration alone whether a blocking artifact exists."""
        ...

    async def remove_artifact(self, agent: Agent, artifact: Artifact) -> None:
        """Remove one enumerated artifact; a write, so it may raise. A missing collaborator raises
        ``AdapterError`` (``artifact_unremovable``). The route's policy loop owns the consequence:
        ``BLOCKING`` aborts the delete, ``BEST_EFFORT`` logs named residue and proceeds."""
        ...
