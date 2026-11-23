"""The Databricks platform adapter: a thin wrapper over ``DatabricksIdentityService`` (provision,
status, artifact removal) and over the tenant probe, which arrives as an injected callable because
``TenantService`` itself calls this seam (a service reference would be a cycle).

Both collaborators are optional: a control plane can be deployed with no Databricks wiring. A write
with none wired raises ``AdapterError``; a read degrades to ``unknown`` with a fixed sentence
composed here, never from an upstream exception (which can carry a workspace path or a response
body). The probe refuses rather than degrading because its result is persisted as the tenant's
``binding_mode``. ``RuntimeStatusWithEnv`` and the identity service are imported inside the
methods that need them so this module never loads the AgentCore service graph.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from models.agent import UNKNOWN_STAGE, RuntimeStatusValue
from models.tenant import TenantPlatform
from services.platform_adapter import AdapterError
from services.platform_registry import declaration_for

if TYPE_CHECKING:
    from api.routes.agents import InvokeRequest
    from core.rbac import Principal
    from models.agent import Agent
    from models.tenant import Tenant
    from services.agent_identity_service import RuntimeStatusWithEnv
    from services.databricks_identity_service import DatabricksIdentityService
    from services.platform_adapter import Artifact
    from services.platform_registry import PlatformDeclaration

logger = logging.getLogger(__name__)

TenantProbe = Callable[["Tenant", tuple[str | None, str | None]], "tuple[dict, str]"]

_NO_READER_DETAIL = "runtime status could not be read (no Databricks reader is configured)"
_READ_FAILED_DETAIL = "runtime status could not be read (the workspace could not be reached)"
_NOT_DEPLOYED_DETAIL = "no runtime has been deployed yet"


class DatabricksPlatformAdapter:
    """AGP's verbs against Databricks, delegated to ``DatabricksIdentityService``. A write refuses
    loudly without a collaborator, a read degrades honestly, and the probe refuses."""

    def __init__(
        self,
        databricks_identity: DatabricksIdentityService | None = None,
        *,
        probe: TenantProbe | None = None,
    ) -> None:
        self._databricks_identity = databricks_identity
        self._probe = probe

    def declaration(self) -> PlatformDeclaration:
        return declaration_for(TenantPlatform.DATABRICKS)

    async def probe(self, tenant: Tenant, admin_creds: tuple[str, str] | None = None) -> tuple[dict, str]:
        """Reach the tenant's workspaces and reduce to ``(capabilities, binding_mode)``.

        Refuses with ``AdapterError`` when no probe was injected: the pair is persisted, and the
        AWS answer ``({}, "")`` would claim no binding is needed on the one field the Databricks
        invoke path depends on. ``admin_creds`` is normalised to the two-slot pair the tenant
        probe takes. Async because the protocol is; the probe itself is sync by ``TenantService``'s
        design.
        """
        if self._probe is None:
            raise AdapterError("AGP has no Databricks tenant probe wired", kind="platform_unsupported")
        return self._probe(tenant, admin_creds or (None, None))

    async def provision_runtime_binding(self, agent: Agent) -> None:
        """Mirror the grant into the workspace's own ACL. Raises for a missing collaborator rather
        than skipping: the workspace enforces whatever permission model it holds, so an agent
        marked provisioned whose ACL was never written is a governance claim with nothing behind
        it. The collaborator's ``ProvisioningError`` passes through untranslated."""
        if self._databricks_identity is None:
            raise AdapterError("AGP has no Databricks provisioner wired", kind="platform_unsupported")
        await self._databricks_identity.provision_databricks_runtime(agent)

    def runtime_status(self, agent: Agent, stage: str | None = None) -> RuntimeStatusWithEnv:
        """One Databricks app's live status. Never raises.

        Local evidence first: a record with no ``runtime_handle`` was never bound to anything and
        reads ``not_deployed`` without a reader. No reader, or a reader that raises, degrades to
        ``unknown`` (never ``not_deployed``, a positive claim, and never ``failed``, which only the
        producer can see). Every path re-wraps as ``RuntimeStatusWithEnv`` with
        ``environment_variables=None``: a Databricks app holds no backend-injected MCP env, so the
        reconciler has nothing to heal.
        """
        from services.agent_identity_service import RuntimeStatusWithEnv

        def _result(status: RuntimeStatusValue, detail: str) -> RuntimeStatusWithEnv:
            return RuntimeStatusWithEnv(
                agent_id=agent.id,
                stage=stage or UNKNOWN_STAGE,
                status=status,
                checked_at=datetime.now(UTC).isoformat(),
                detail=detail,
            )

        if not (agent.runtime_handle or "").strip():
            return _result("not_deployed", _NOT_DEPLOYED_DETAIL)
        if self._databricks_identity is None:
            return _result("unknown", _NO_READER_DETAIL)
        try:
            status = self._databricks_identity.runtime_status(agent, stage=stage)
        except Exception:
            logger.warning(
                "[platform_adapter] Databricks status read failed for agent %s", agent.id, exc_info=True
            )
            return _result("unknown", _READ_FAILED_DETAIL)
        return RuntimeStatusWithEnv(**status.model_dump())

    async def invoke(
        self,
        agent: Agent,
        req: InvokeRequest,
        principal: Principal,
        obo_token: str,
        stage: str | None = None,
        actor_id: str | None = None,
    ) -> dict:
        """The Databricks App invoke leg, delegated to ``services.databricks_invoke``.

        No collaborator guard: the leg resolves its collaborators at call time through the route
        module's accessors, and its own codes (``sp_credential_unavailable`` when no identity
        service is wired) are more specific than ``platform_unsupported`` would be. ``stage`` is
        ignored because a Databricks App has no per-stage runtime identity; ``session_id`` and
        ``tool_calls`` are ``None`` because the platform has neither.
        """
        from services.databricks_invoke import invoke_databricks

        result = await invoke_databricks(agent, req, principal, obo_token)
        return {**result, "session_id": None, "tool_calls": None}

    def named_artifacts(self, agent: Agent) -> list[Artifact]:
        """The record's Databricks residue, read off its own fields by a static method so it answers
        without a collaborator."""
        from services.databricks_identity_service import DatabricksIdentityService

        return DatabricksIdentityService.named_databricks_artifacts(agent)

    async def remove_artifact(self, agent: Agent, artifact: Artifact) -> None:
        """Remove one enumerated artifact through the service; without one, ``artifact_unremovable``
        so the route refuses to delete a record that still names a blocking artifact."""
        if self._databricks_identity is None:
            raise AdapterError("AGP has no Databricks provisioner wired", kind="artifact_unremovable")
        await self._databricks_identity.remove_databricks_artifact(agent, artifact)
