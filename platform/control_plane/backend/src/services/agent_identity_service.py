"""Entra identity provisioning for an AgentCore agent, and the runtime calls that go with it.

``AgentIdentityService`` ties ``GraphService`` (Entra, Graph, OBO) to ``AgentRegistryService``
(the record and its ``persist_identity`` write). ``provision`` mints the per-agent Entra app and
service principal, persists the identity fields at once, requires an app-role assignment, wires
the backend-to-agent OBO consent, then configures every runtime's inbound JWT authorizer off the
event loop and flips ``identity_status`` to ``provisioned``. It is idempotent and resumable: every
step tolerates a re-run, the create is skipped when the record already carries an
``entra_sp_id``, and any failure persists ``identity_status='failed'`` and raises
``ProvisioningError``. The same class owns the runtime's status read, its env merge and its
teardown.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Awaitable, Callable, ClassVar, Optional

import anyio
import anyio.to_thread
from botocore.exceptions import BotoCoreError, ClientError

from core import aws_clients
from core.errors import AppError, Outcome
from models.agent import (
    UNKNOWN_STAGE,
    Agent,
    IdentityStatus,
    RuntimeStatus,
    RuntimeStatusValue,
    is_harness_arn,
)
from services.agent_registry_service import AgentRegistryService, _is_not_found
from services.graph_errors import GraphError
from services.graph_service import GraphService

logger = logging.getLogger(__name__)

_POLL_MAX_ATTEMPTS = 60
_POLL_INTERVAL_SECONDS = 5.0

_READY_STATUS = "READY"
_FAILED_STATUSES = frozenset({"CREATE_FAILED", "UPDATE_FAILED", "DELETE_FAILED", "FAILED"})


_NATIVE_TO_RUNTIME_STATUS: dict[str, RuntimeStatusValue] = {
    "READY": "ready",
    "CREATING": "creating",
    "UPDATING": "updating",
    "CREATE_FAILED": "failed",
    "UPDATE_FAILED": "failed",
    "DELETE_FAILED": "failed",
    "FAILED": "failed",
    "DELETING": "unknown",
}

_SAFE_ERROR_CODE = re.compile(r"^[A-Za-z]{1,64}$")

_PROBE_UNREACHABLE_DETAIL = "runtime status could not be read (control plane unreachable)"


def is_agentcore_agent(agent: Agent) -> bool:
    """The provisioning gate: a manually created AgentCore Runtime agent, one with an ``agent_arn``,
    ``auth_type == ENTRA`` and ``platform == AWS_BEDROCK``. Delegates to ``Agent.is_agentcore``.
    """
    return agent.is_agentcore


def _safe_probe_detail(err: ClientError) -> str:
    """A safe hint for a failed status probe: the error code alone, and only when it matches
    ``_SAFE_ERROR_CODE``. An AgentCore AccessDenied message names the assumed-role ARN with its
    account id, so the message never reaches a response body.
    """
    code = err.response.get("Error", {}).get("Code") or ""
    if _SAFE_ERROR_CODE.match(code):
        return f"runtime status could not be read ({code})"
    return _PROBE_UNREACHABLE_DETAIL


def _image_tag(runtime: dict) -> str | None:
    """The deployed image tag from a ``get_agent_runtime`` response, or None. The container URI
    carries the account id, so only the segment after its last ``:`` is returned; an untagged URI
    yields None.
    """
    uri = (runtime.get("agentRuntimeArtifact") or {}).get("containerConfiguration", {}).get("containerUri")
    if not isinstance(uri, str) or ":" not in uri.rsplit("/", 1)[-1]:
        return None
    return uri.rsplit(":", 1)[-1] or None


class RuntimeStatusWithEnv(RuntimeStatus):
    """A ``RuntimeStatus`` that also carries the runtime's live env, for callers inside the platform.

    A dict is evidence from a read that reached the runtime (``{}`` means its env is empty, which
    is what a replacement leaves behind); ``None`` means the read never got an answer about the env,
    so it says nothing about the wiring and justifies no heal. The subclass keeps the env out of the
    wire: ``GET /agents/{id}/runtime`` declares ``response_model=RuntimeStatus``, so the env is
    dropped before serialization.
    """

    environment_variables: dict[str, str] | None = None


RuntimeBinding = Callable[[Agent], Awaitable[None]]


class ProvisioningError(AppError):
    """A step of the non-atomic Entra and platform provisioning or teardown sequence failed.

    ``provision`` persists ``identity_status='failed'`` before raising it, so a re-provision
    can resume. The one kind renders the only wire this class reaches: the delete route refuses
    to remove a record whose blocking platform artifact, the Databricks federation audience,
    could not be withdrawn, with a fixed detail. Every other raise site is caught before a
    client sees it (the persisted 'failed' status, a teardown report item, an ``InvokeError``
    kind), and the message is for the log.
    """

    domain: ClassVar[str] = "identity"
    kinds: ClassVar[Mapping[str, Outcome]] = {
        "artifact_removal_failed": Outcome(
            502,
            "the agent's Databricks federation audience could not be removed, so the agent was "
            "not deleted — retry, or ask your Databricks account admin",
        ),
    }


class AgentIdentityService:
    """Orchestrates Entra identity provisioning for an AgentCore agent."""

    def __init__(
        self,
        *,
        graph: GraphService,
        registry: AgentRegistryService,
        control_client=None,
        region: str | None = None,
        tenant_id: str,
        login_base: str,
        credentials=None,
    ) -> None:
        self._graph = graph
        self._registry = registry
        self._region = region
        self._tenant_id = tenant_id
        self._login_base = login_base.rstrip("/")
        self._credentials = credentials
        self._control = control_client or aws_clients.client("bedrock-agentcore-control", region=region)

    async def provision(self, agent: Agent, *, runtime_binding: "Optional[RuntimeBinding]" = None) -> Agent:
        """Idempotent, resumable provisioning: ``provision_identity``, then the runtime half.

        ``runtime_binding`` is the caller's platform adapter's ``provision_runtime_binding``; when given
        it is the runtime half, and it is called inside this envelope so an unwired platform lands as a
        persisted 'failed' instead of a record stranded at 'pending'. Without one, this service's own
        AgentCore arm runs once the record names a runtime (``runtime_arns()`` reads the scalar and the
        per-stage map alike); a record naming none defers the runtime half, the pre-registration state.
        Any failure persists ``identity_status='failed'`` and raises ``ProvisioningError`` from the
        original error; a ``GraphError``'s text is logged because it carries only a status, a code and
        a resource endpoint's safe message.
        """
        try:
            await self.provision_identity(agent)

            if runtime_binding is not None:
                await runtime_binding(agent)
            elif agent.runtime_arns():
                await self.provision_runtime(agent)
            return agent

        except Exception as err:
            agent.identity_status = IdentityStatus.FAILED
            try:
                await anyio.to_thread.run_sync(self._registry.persist_identity, agent)
            except Exception:
                logger.exception(
                    "[agent_identity] failed to persist 'failed' status for agent %s",
                    agent.id,
                )
            detail = err if isinstance(err, GraphError) else type(err).__name__
            logger.warning(
                "[agent_identity] provisioning failed for agent %s: %s",
                agent.id,
                detail,
            )
            raise ProvisioningError(f"provisioning failed for agent {agent.id}") from err

    async def provision_identity(self, agent: Agent) -> Agent:
        """Mint the Entra identity only: the per-agent app registration and service principal, skipped
        when the record already carries an ``entra_sp_id`` and persisted at once so a later failure
        cannot make the next run re-create the app; then the assignment requirement and the
        backend-to-agent OBO consent. Touches no runtime, so it is safe before the runtime exists.

        ``entra_app_audience`` is the ``api://`` URI AgentCore's authorizer accepts; the OBO token's
        ``aud`` is the client-id GUID, which the Databricks federation half appends from
        ``entra_app_id``. A create that returns no ``sp_id`` raises before step 2, with the app ids
        already set so the next run resolves the existing app instead of re-creating it.
        """
        if not agent.entra_sp_id:
            created = await self._graph.create_agent_app(agent.id, agent.name)
            agent.entra_app_id = created["app_id"]
            agent.entra_sp_id = created["sp_id"]
            agent.invoker_role_id = created["invoker_role_id"]
            agent.admin_role_id = created["admin_role_id"]
            agent.entra_app_audience = created["app_uri"]
            agent.identity_status = IdentityStatus.PENDING
            if not agent.entra_sp_id:
                raise ProvisioningError(f"create_agent_app returned no sp_id for agent {agent.id}")
            await anyio.to_thread.run_sync(self._registry.persist_identity, agent)

        await self._graph.set_assignment_required(agent.entra_sp_id)

        await self._graph.grant_backend_obo_consent(agent.entra_sp_id)

        return agent

    async def provision_runtime(self, agent: Agent) -> Agent:
        """Wire the inbound JWT authorizer on every runtime the agent owns, then flip to 'provisioned'.
        Requires at least one runtime (``ValueError`` otherwise). Every runtime, because one with no
        ``authorizerConfiguration`` accepts unauthenticated invocations. The fan-out is sync boto3 with
        a poll per runtime, dispatched off the event loop once.
        """
        if not agent.runtime_arns():
            raise ValueError("provision_runtime requires agent.agent_arn or agent.agent_arns")

        await anyio.to_thread.run_sync(self._configure_runtime_authorizers, agent)

        agent.identity_status = IdentityStatus.PROVISIONED
        await anyio.to_thread.run_sync(self._registry.persist_identity, agent)
        return agent

    def _credential_service(self):
        """The ``AgentCredentialService`` for the OBO-provider teardown, built on first use from this
        service's own Graph client, control client and region.
        """
        if self._credentials is None:
            from services.agent_credential_service import AgentCredentialService

            self._credentials = AgentCredentialService(
                graph=self._graph, control_client=self._control, region=self._region
            )
        return self._credentials

    async def delete_obo_provider(self, agent_id: str) -> None:
        """Delete the agent's ``agp-agent-obo-{id}`` Token Vault entry, propagating failure. Idempotent,
        since the deleter swallows not-found; the name derives from the id alone.
        """
        await self._credential_service().delete_agent_obo_provider(agent_id)

    async def delete_identity(self, agent: Agent, *, include_obo_provider: bool = True) -> None:
        """Tear down the agent's Entra app and service principal and, by default, its OBO credential
        provider first, since the app's deletion is what makes the vault entry dangling.

        The provider leg's failure is logged and swallowed: in the repo cascade the identity item is
        blocking, and a raise would trap the record behind a vault entry a retry of that cascade cannot
        fix. ``include_obo_provider=False`` lets a cascade that reports the provider as its own item
        skip the bundled leg. Idempotent: ``delete_agent_app`` swallows 404 and no-ops on blank ids.
        """
        if include_obo_provider:
            try:
                await self._credential_service().delete_agent_obo_provider(agent.id)
            except Exception:
                logger.exception(
                    "[agent_identity] OBO credential provider teardown best-effort no-op for "
                    "agent %s (the Token Vault entry may survive; a re-run reclaims it)",
                    agent.id,
                )
        await self._graph.delete_agent_app(entra_app_id=agent.entra_app_id, entra_sp_id=agent.entra_sp_id)

    def delete_runtime(self, agent_arn: str, *, control_client=None) -> None:
        """Delete the agent's AgentCore Runtime (sync boto3; the caller dispatches off the loop).

        A falsy or harness ARN means nothing to delete. The runtime is probed first and the delete
        skipped only on a genuine ``ResourceNotFoundException``; AccessDenied is ambiguous, since a live
        runtime behind a misconfigured policy returns it too, so the delete is attempted and a real
        failure propagates. ``control_client`` selects the account for both the probe and the delete; a
        probe in another account would report a live runtime as gone.
        """
        if not agent_arn or is_harness_arn(agent_arn):
            return
        client = control_client or self._control
        try:
            if not self.runtime_exists(agent_arn, control_client=client):
                return
        except ClientError as err:
            if _is_not_found(err):
                return
        runtime_id = agent_arn.rsplit("/", 1)[-1]
        try:
            client.delete_agent_runtime(agentRuntimeId=runtime_id)
        except ClientError as err:
            if _is_not_found(err):
                return
            raise

    def runtime_exists(self, agent_arn: str, *, control_client=None) -> bool:
        """Whether the AgentCore Runtime still exists (sync boto3). False for a falsy or harness ARN and
        for a genuine ``ResourceNotFoundException``; any other ``ClientError`` propagates so the caller
        treats it as unknown rather than gone. ``control_client`` selects the account to ask; a
        not-found is trustworthy only from the account that owns the runtime.
        """
        if not agent_arn or is_harness_arn(agent_arn):
            return False
        client = control_client or self._control
        runtime_id = agent_arn.rsplit("/", 1)[-1]
        try:
            client.get_agent_runtime(agentRuntimeId=runtime_id)
            return True
        except ClientError as err:
            if _is_not_found(err):
                return False
            raise

    def runtime_status(self, agent: Agent, stage: str | None = None) -> RuntimeStatusWithEnv:
        """One agent's live runtime status (sync boto3; the caller dispatches off the loop). Never raises:
        a genuine not-found is ``not_deployed``, any other ``ClientError`` and any transport error is
        ``unknown`` with a safe hint, never ``failed``.

        ``stage=None`` probes the runtime ``agent_arn`` names and attributes the stage from the record's
        map when it can (``UNKNOWN_STAGE`` for a scalar-only record); a given stage probes that stage's
        runtime, or answers ``not_deployed`` when the record names none. A record with no runtime, and a
        harness, answer locally. The env travels on ``RuntimeStatusWithEnv``: a dict when the read
        reached the runtime, ``None`` on every degraded path.
        """
        checked_at = datetime.now(timezone.utc).isoformat()
        arns = agent.runtime_arns()

        if stage is None:
            arn = agent.agent_arn or next(iter(arns.values()), None)
            resolved_stage = next((s for s, a in arns.items() if a == arn), UNKNOWN_STAGE)
        else:
            arn = arns.get(stage)
            resolved_stage = stage

        def _result(status: RuntimeStatusValue, **extra) -> RuntimeStatusWithEnv:
            return RuntimeStatusWithEnv(
                agent_id=agent.id,
                stage=resolved_stage,
                status=status,
                runtime_arn=arn,
                checked_at=checked_at,
                **extra,
            )

        if not arn:
            return _result("not_deployed", detail="no runtime has been deployed yet")
        if is_harness_arn(arn):
            return _result("unknown", detail="status not tracked for config agents")

        runtime_id = arn.rsplit("/", 1)[-1]
        try:
            resp = self._control.get_agent_runtime(agentRuntimeId=runtime_id)
        except ClientError as err:
            if _is_not_found(err):
                return _result("not_deployed", detail="the runtime no longer exists")
            return _result("unknown", detail=_safe_probe_detail(err))
        except BotoCoreError:
            logger.warning(
                "[runtime-status] control-plane read failed for agent %s",
                agent.id,
                exc_info=True,
            )
            return _result("unknown", detail=_PROBE_UNREACHABLE_DETAIL)

        native = resp.get("status")
        status = _NATIVE_TO_RUNTIME_STATUS.get(native, "unknown")
        detail = None
        if status == "unknown":
            detail = (
                f"runtime is {native.lower()}"
                if isinstance(native, str) and _SAFE_ERROR_CODE.match(native.replace("_", ""))
                else "the runtime reported no recognizable status"
            )
        live_env = resp.get("environmentVariables")
        return _result(
            status,
            detail=detail,
            image_tag=_image_tag(resp),
            environment_variables=live_env if isinstance(live_env, dict) else {},
        )

    def _configure_runtime_authorizers(self, agent: Agent) -> None:
        """Wire the inbound JWT authorizer on every runtime the agent owns (sync). Attempt all, then
        raise: one unreachable runtime does not stop the others, and the fan-out raises at the end
        because an agent reported provisioned while one runtime accepts unauthenticated invocations is
        the dangerous outcome. A harness wires its own authorizer in Terraform and is skipped.
        """
        arns = agent.runtime_arns()
        failed: list[str] = []
        for stage, arn in arns.items():
            if is_harness_arn(arn):
                continue
            try:
                self._configure_runtime_authorizer(arn, agent.entra_app_audience, agent.entra_app_id)
            except Exception:
                logger.exception(
                    "[agent_identity] authorizer config failed for agent %s stage %s",
                    agent.id,
                    stage,
                )
                failed.append(stage)
        if failed:
            raise RuntimeError(
                f"the inbound authorizer could not be wired on {len(failed)} of "
                f"{len(arns)} runtimes of agent {agent.id}: {sorted(failed)}"
            )

    def _configure_runtime_authorizer(
        self, agent_arn: str, agent_audience: str | None, agent_client_id: str | None = None
    ) -> None:
        """Set one runtime's inbound JWT authorizer (sync boto3). The control calls take
        ``agentRuntimeId``, the ARN's last ``/`` segment. ``UpdateAgentRuntime`` is a full replace, so
        the required fields are replayed from ``get_agent_runtime`` with the authorizer added, then the
        runtime is polled to READY. Both audience forms are allowed, since the OBO token's ``aud`` may
        be the client-id GUID or the ``api://`` URI.
        """
        runtime_id = agent_arn.rsplit("/", 1)[-1]

        current = self._control.get_agent_runtime(agentRuntimeId=runtime_id)

        authorizer_configuration = {
            "customJWTAuthorizer": {
                "discoveryUrl": (
                    f"{self._login_base}/{self._tenant_id}/v2.0/.well-known/openid-configuration"
                ),
                "allowedAudience": [a for a in (agent_audience, agent_client_id) if a],
            }
        }

        self._control.update_agent_runtime(
            agentRuntimeId=runtime_id,
            agentRuntimeArtifact=current["agentRuntimeArtifact"],
            roleArn=current["roleArn"],
            networkConfiguration=current["networkConfiguration"],
            authorizerConfiguration=authorizer_configuration,
        )

        self._poll_runtime_ready(runtime_id)

    def set_runtime_environment(
        self,
        agent_arn: str,
        env: dict[str, str],
        *,
        wait_ready: bool = True,
        control_client=None,
    ) -> str | None:
        """Merge container env vars onto an existing AgentCore Runtime (sync boto3) and return its
        execution ``roleArn``.

        ``UpdateAgentRuntime`` is a full replace, so the required fields, the inbound authorizer and
        every optional field present are replayed from the GET; the env is the runtime's current one
        merged with ``env``, new keys winning, on the top-level ``environmentVariables`` map. Idempotent.
        ``wait_ready`` polls to READY, the grant path's contract; the reconcile-on-read and Langfuse
        callers pass False, since the poll would park a shared worker thread for up to five minutes per
        runtime and a non-converged update is observed by the next read anyway. ``control_client``
        selects the account for the GET, the update and the poll alike. A harness has no env to merge
        into and returns None.
        """
        if is_harness_arn(agent_arn):
            return None
        runtime_id = agent_arn.rsplit("/", 1)[-1]
        client = control_client or self._control

        current = client.get_agent_runtime(agentRuntimeId=runtime_id)

        merged_env = {**(current.get("environmentVariables") or {}), **env}

        kwargs: dict = {
            "agentRuntimeId": runtime_id,
            "agentRuntimeArtifact": current["agentRuntimeArtifact"],
            "roleArn": current["roleArn"],
            "networkConfiguration": current["networkConfiguration"],
            "environmentVariables": merged_env,
        }
        for key in (
            "authorizerConfiguration",
            "authorizerType",
            "protocolConfiguration",
            "requestHeaderConfiguration",
            "lifecycleConfiguration",
            "metadataConfiguration",
            "filesystemConfigurations",
            "description",
        ):
            if current.get(key):
                kwargs[key] = current[key]

        client.update_agent_runtime(**kwargs)

        if wait_ready:
            self._poll_runtime_ready(runtime_id, client=client)

        return current["roleArn"]

    def _poll_runtime_ready(self, runtime_id: str, *, client=None) -> None:
        """Poll ``get_agent_runtime`` until READY; a ``*_FAILED`` status or ``_POLL_MAX_ATTEMPTS``
        exhausted raises ``RuntimeError``. ``client`` polls under the same client that wrote.
        """
        control = client or self._control
        for _ in range(_POLL_MAX_ATTEMPTS):
            resp = control.get_agent_runtime(agentRuntimeId=runtime_id)
            status = resp.get("status")
            if status == _READY_STATUS:
                return
            if status in _FAILED_STATUSES:
                raise RuntimeError(f"runtime {runtime_id} reached terminal status {status}")
            time.sleep(_POLL_INTERVAL_SECONDS)
        raise RuntimeError(f"runtime {runtime_id} did not reach READY within {_POLL_MAX_ATTEMPTS} polls")
