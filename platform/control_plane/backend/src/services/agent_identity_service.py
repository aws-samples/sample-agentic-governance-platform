"""Entra identity provisioning orchestrator (Epic 6, Task T-IDENTITY).

``AgentIdentityService`` ties the two E6 collaborators together for a manually-created
AgentCore Runtime agent at REGISTRATION time (as a background step):

  - ``GraphService`` (T-GRAPH) owns all Microsoft Entra / Graph / OBO HTTP.
  - ``AgentRegistryService`` (E4) owns the registry record CRUD + the
    ``persist_identity`` envelope-write.

For an agent that passes the gate (``is_agentcore_agent``), :meth:`provision`:
  1. creates the per-agent Entra app reg + SP (Invoke scope + Invoker/Admin roles),
     sets the 5 identity fields + ``identity_status='pending'`` on the record and
     **persists them IMMEDIATELY** (CRITIQUE-FIX-A) before any later step,
  2. sets the agent SP to assignment-required,
  3. wires backend→agent OBO delegated consent,
  4. configures the runtime's inbound JWT authorizer (GET→replay→UpdateAgentRuntime,
     poll-to-READY) — dispatched OFF the event loop (CRITIQUE-FIX-B),
  5. flips ``identity_status='provisioned'`` and persists.

It is **idempotent + resumable**: steps 2/3/4 are individually idempotent (the Graph
adapter's ``set_assignment_required`` PATCH is idempotent, ``grant_backend_obo_consent``
swallows already-exists, and the authorizer GET→Update is idempotent), and step 1 is
skipped when the agent already carries an ``entra_sp_id`` (a re-provision). On ANY
failure it persists ``identity_status='failed'`` and raises :class:`ProvisioningError`.

Mechanics source: research §1 (the ``GetAgentRuntime`` → replay
``agentRuntimeArtifact`` / ``roleArn`` / ``networkConfiguration`` + add
``customJWTAuthorizer`` via ``UpdateAgentRuntime`` → poll to READY; the AUDIENCE-FORM
finding; ``RID``-not-ARN) and §0 (registration-time, background, AgentCore-only gate).
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone

import anyio
import anyio.to_thread
import boto3
from botocore.exceptions import BotoCoreError, ClientError

from models.agent import (
    UNKNOWN_STAGE,
    Agent,
    IdentityStatus,
    RuntimeStatus,
    is_databricks_governed_agent,
)
from services.agent_registry_service import AgentRegistryService, _is_not_found
from services.graph_service import GraphError, GraphService

logger = logging.getLogger(__name__)

# Poll-to-READY loop bounds (research §1.4 — UpdateAgentRuntime is async; the DEFAULT
# endpoint advances through UPDATING→READY, ~minute+). Bounded so a stuck/never-READY
# runtime fails loudly instead of hanging the background task forever.
_POLL_MAX_ATTEMPTS = 60
_POLL_INTERVAL_SECONDS = 5.0

# Native AgentCore runtime statuses (research §1.1). READY is success; a *_FAILED
# (or exhausting the loop) is a provisioning failure.
_READY_STATUS = "READY"
_FAILED_STATUSES = frozenset(
    {"CREATE_FAILED", "UPDATE_FAILED", "DELETE_FAILED", "FAILED"}
)


# E28/T5 (design D9): native AgentCore status → the C2 closed union. This makes the
# mapping that was previously IMPLICIT in the provisioning paths (READY = success,
# `_FAILED_STATUSES` = failure) explicit and readable, which is the whole of T5 — the
# status already existed, only its exposure was missing.
#
# Keys are the botocore `AgentRuntimeStatus` enum EXACTLY (verified against the loaded
# service model by a test, so a status AgentCore adds later fails offline instead of
# silently degrading a live runtime to "unknown" in the UI).
#
# `DELETING` maps to "unknown", NOT "failed": a deliberate teardown is not a fault, and
# C2's union has no slot for it (a 7th value would break the frontend's no-default
# `Record<RuntimeStatusKey, …>` tables — C3). It carries a truthful hint instead.
#
# `DELETE_FAILED` / `FAILED` are NOT in the botocore enum but ARE in `_FAILED_STATUSES`
# above; that defensive pair predates T5 and is kept mapped so the two sets cannot
# disagree about what "failed" means.
_NATIVE_TO_RUNTIME_STATUS = {
    "READY": "ready",
    "CREATING": "creating",
    "UPDATING": "updating",
    "CREATE_FAILED": "failed",
    "UPDATE_FAILED": "failed",
    "DELETE_FAILED": "failed",
    "FAILED": "failed",
    "DELETING": "unknown",
}

# A boto3 error CODE is the ONLY part of a ClientError safe to surface, and only when it
# LOOKS like a code. A real AgentCore AccessDenied MESSAGE names the assumed-role ARN
# (account id included) and can quote the caller's session — a hard project rule bans an
# account id anywhere, tests and error strings included. This pattern is deliberately
# narrow so the upstream cannot smuggle a payload through the `Code` field either.
_SAFE_ERROR_CODE = re.compile(r"^[A-Za-z]{1,64}$")

# What the UI is told when the probe itself failed. Deliberately says the PROBE failed,
# never that the runtime did.
_PROBE_UNREACHABLE_DETAIL = "runtime status could not be read (control plane unreachable)"


# E23/T11 (AccessDenied-ambiguity fix): a "gone" inference keys ONLY on a genuine
# ``ResourceNotFoundException`` (``_is_not_found``, imported from agent_registry_service).
# AgentCore may return AccessDenied for a missing runtime, but a LIVE runtime behind an
# IAM/SCP/wrong-region misconfig returns the SAME code — so AccessDenied is AMBIGUOUS and
# must NEVER be inferred as gone (that would silently orphan a live runtime + irreversibly
# delete its record). With the broad ``bedrock-agentcore:*`` grant (T7) a real gone-runtime
# returns cleanly or NotFound; a genuine AccessDenied is a real problem that must surface.


def is_agentcore_agent(agent: Agent) -> bool:
    """The provisioning gate: who gets an Entra identity (research §0, Decision 5).

    True only for a manually-created AgentCore Runtime agent — it carries an
    ``agent_arn`` AND ``auth_type == ENTRA`` AND ``platform == AWS_BEDROCK``. The
    ~18 metadata-only seed agents (no ARN) and non-Entra / non-Bedrock records are
    skipped.

    Thin wrapper that delegates to ``Agent.is_agentcore`` (the single source of truth on
    the model) so the boolean logic lives in exactly one place; kept as a module-level
    function so existing callers (the route's import + tests) don't churn.
    """
    return agent.is_agentcore


def _safe_probe_detail(err: ClientError) -> str:
    """A SAFE short hint for a failed status probe (E28/T5) — never the upstream message.

    Only the error CODE is surfaced, and only when it matches `_SAFE_ERROR_CODE`. A real
    AgentCore AccessDenied message reads `User: arn:aws:sts::<account>:assumed-role/… is
    not authorized to perform: … on arn:aws:bedrock-agentcore:…` — an ARN, an AWS account
    id, and sometimes a session reference. None of that may reach a response body (the
    project's no-account-ids rule) and passing an upstream body through is how credentials
    leak into a UI. Anything unrecognizable falls back to the fixed hint.
    """
    code = err.response.get("Error", {}).get("Code") or ""
    if _SAFE_ERROR_CODE.match(code):
        return f"runtime status could not be read ({code})"
    return _PROBE_UNREACHABLE_DETAIL


def _image_tag(runtime: dict) -> str | None:
    """The deployed image TAG from a `get_agent_runtime` response, or None (E28/T5).

    The container URI is `<account>.dkr.ecr.<region>.amazonaws.com/<repo>:<tag>` — it
    CARRIES the AWS account id, so the URI itself must never be returned. Only the segment
    after the LAST ':' is safe, and an untagged URI (no ':') yields None rather than the
    registry host. `rsplit` on ':' is correct even for a port-bearing registry, because the
    tag is always last.
    """
    uri = (
        (runtime.get("agentRuntimeArtifact") or {})
        .get("containerConfiguration", {})
        .get("containerUri")
    )
    if not isinstance(uri, str) or ":" not in uri.rsplit("/", 1)[-1]:
        return None
    return uri.rsplit(":", 1)[-1] or None


class RuntimeStatusWithEnv(RuntimeStatus):
    """A :class:`RuntimeStatus` that also carries the runtime's LIVE env, for callers INSIDE
    the platform (E36/T12).

    ``get_agent_runtime`` returns ``environmentVariables`` and
    :meth:`AgentIdentityService.runtime_status` used to discard them;
    ``reconcile_runtime_mcp_env`` needs exactly that dict to tell a wiped runtime from a wired
    one, so carrying it here makes the detection cost ZERO extra AWS calls.

    TWO-VALUED, AND THE DIFFERENCE IS LOAD-BEARING (fix round 1). A ``dict`` is EVIDENCE from a
    read that reached the runtime — ``{}`` means "its env is empty", which is exactly what a
    replacement leaves behind. ``None`` means the read never got an answer about the env
    (nothing deployed, ``ResourceNotFoundException``, Throttling/AccessDenied, transport
    failure), so it says NOTHING about the wiring. Only the first can justify a heal: with the
    two collapsed, every degraded read looked like a wipe and the reconciler fired on it
    forever — turning a throttle into an extra write against the throttled API, and a
    cross-account (unreachable) runtime into a guaranteed failing write per read.

    IT IS NOT PART OF THE READ CONTRACT, DELIBERATELY. The subclass — rather than a field on
    ``RuntimeStatus`` — is what keeps it out of the wire format: ``GET /agents/{id}/runtime``
    declares ``response_model=RuntimeStatus``, so FastAPI validates this object INTO that model
    and the env is dropped before serialization. A runtime env can carry an audience, an ARN or a
    provider name, and that route is VIEWER-gated (the loosest gate in the product), so the env
    must never be serialized to it — and ``RuntimeStatus``' own contract ("NO other field may
    carry [the account id]") stays literally true.
    """

    environment_variables: dict[str, str] | None = None


class ProvisioningError(Exception):
    """A step in the (non-atomic) Entra+AWS provisioning sequence failed.

    The agent's ``identity_status`` is persisted as ``'failed'`` before this is
    raised, so a later re-provision (idempotent + resumable) can retry.
    """


class AgentIdentityService:
    """Orchestrates Entra identity provisioning for an AgentCore agent."""

    def __init__(
        self,
        *,
        graph: GraphService,
        registry: AgentRegistryService,
        control_client=None,
        region: str = "us-east-1",
        tenant_id: str,
        login_base: str,
        credentials=None,
        databricks_identity=None,
    ) -> None:
        self._graph = graph
        self._registry = registry
        # E29/T6: the DATABRICKS half of provisioning, injected. Optional so every AgentCore
        # construction site is unchanged — but its absence is NOT a silent skip: the dispatch
        # below raises when a Databricks agent arrives with no collaborator wired. A skip
        # would strand the agent at 'pending' with no error, which is exactly the failure
        # shape E28A/T1 had to hunt down on the AgentCore side.
        self._databricks_identity = databricks_identity
        self._region = region
        self._tenant_id = tenant_id
        self._login_base = login_base.rstrip("/")
        # E36/T16: the AgentCredentialService whose per-agent OBO credential provider
        # (``agp-agent-obo-{id}``) :meth:`delete_identity` now tears down. Optional and
        # LAZILY built (see :meth:`_credential_service`) so no caller needs a wiring change
        # for a teardown-only collaborator; injectable for tests.
        self._credentials = credentials
        # boto3 ``bedrock-agentcore-control`` client. Injectable for tests (a
        # MagicMock); built by default (no live AWS in unit tests, which always
        # inject). Control-plane signing name is ``bedrock-agentcore-control``.
        #
        # E36/T8: this client carries the backend's AMBIENT ECS-task credentials, which reach
        # the CONTROL-PLANE account only. It stays the default — deploy-in-place tenants are
        # the shipped shape — but it is no longer assumed to be the right account: the
        # teardown path passes a per-stage ``control_client`` (see :meth:`delete_runtime` /
        # :meth:`runtime_exists`) resolved through ``tenant_credentials.stage_client`` when the
        # stage carries a ``deploy_role_arn``. Every OTHER method here still uses this client
        # unconditionally, which is a real remaining ceiling for cross-account tenants
        # (``set_runtime_environment`` is the one that bites — see the item-3 research).
        self._control = control_client or boto3.client(
            "bedrock-agentcore-control", region_name=region
        )

    # -- public ------------------------------------------------------------

    async def provision(self, agent: Agent) -> Agent:
        """Idempotent, resumable provisioning. See the module docstring for steps.

        The full sequence = :meth:`provision_identity` (mint the Entra identity, steps
        1-3) then :meth:`provision_runtime` (the runtime authorizer, step 4) — but the
        runtime half runs ONLY when the agent names at least one runtime, i.e. when
        ``agent.runtime_arns()`` is non-empty (the ``agent_arn`` scalar OR the per-stage
        ``agent_arns`` map — E28A/T1). For a manually-created AgentCore agent (the
        historical caller) the scalar is always present, so this preserves the pre-split
        behavior exactly; for a pre-registration agent (E20 — identity minted BEFORE the
        runtime exists) the runtime half is deferred until a runtime ARN lands.

        On ANY exception in the sequence: persist ``identity_status='failed'`` and raise
        :class:`ProvisioningError` from the original error.
        """
        try:
            await self.provision_identity(agent)

            # Runtime half — only once a runtime exists (E20 pre-registration mints the
            # identity first, with no ARN yet). When one does, this preserves the pre-split
            # step-4 + flip-to-'provisioned'.
            #
            # E28A/T1: the gate reads `runtime_arns()`, not the `agent_arn` scalar, so it fires
            # when EITHER the scalar or the `agent_arns` MAP names a runtime. With the scalar
            # alone, an agent whose runtimes were known only from the map got ZERO authorizer
            # calls and was stranded 'pending' WITH NO ERROR — the map-support `provision_runtime`
            # advertises in its docstring and its ValueError was unreachable through its only
            # caller. A runtime with no `authorizerConfiguration` accepts UNAUTHENTICATED
            # invocations, so that silence would have shipped an ungoverned copy of a governed
            # agent. C-A2's dual write means the shape cannot occur TODAY, but that is a
            # convention T1b must honour, not a guarantee this gate may depend on.
            #
            # Still correctly FALSE for the pre-registration state (no scalar, empty map ⇒ `{}`),
            # so the legitimate deferral is unchanged and `provision_runtime`'s ValueError stays
            # unreachable from here.
            if agent.runtime_arns():
                await self.provision_runtime(agent)
            # E29/T6, contract C-4: the platform half dispatches by platform. The two gates
            # are mutually exclusive by construction (each pins a different `platform`), and
            # a Databricks agent carries no `agent_arn`, so `runtime_arns()` is empty above
            # and the AgentCore arm never runs for it. Written as `elif` on the SAME chain the
            # registration hook uses, so a future third platform cannot run two runtime halves.
            elif is_databricks_governed_agent(agent):
                if self._databricks_identity is None:
                    raise ProvisioningError(
                        f"agent {agent.id} is Databricks-governed but no Databricks identity "
                        f"service is wired, so its runtime binding cannot be provisioned"
                    )
                await self._databricks_identity.provision_databricks_runtime(agent)
            return agent

        except Exception as err:  # noqa: BLE001 — any failure → failed status + re-raise
            agent.identity_status = IdentityStatus.FAILED
            try:
                self._registry.persist_identity(agent)
            except Exception:  # noqa: BLE001 — don't mask the original failure
                logger.exception(
                    "[agent_identity] failed to persist 'failed' status for agent %s",
                    agent.id,
                )
            # Surface the SAFE failure detail (CRITIQUE-FIX-E). A GraphError's str()
            # now carries status + Graph error code + (for RESOURCE-endpoint errors)
            # a safe message — none of which contain a token or the client secret
            # (those flow only to the /token path, whose errors stay status+code
            # only). For non-Graph errors, log the type name as before. This is the
            # one safe log line that the live incident lacked: it would have shown the
            # real /servicePrincipals 400 code/message instead of just "GraphError".
            detail = err if isinstance(err, GraphError) else type(err).__name__
            logger.warning(
                "[agent_identity] provisioning failed for agent %s: %s",
                agent.id,
                detail,
            )
            raise ProvisioningError(
                f"provisioning failed for agent {agent.id}"
            ) from err

    async def provision_identity(self, agent: Agent) -> Agent:
        """Mint the Entra identity ONLY (the pre-deploy half — steps 1-3).

        Extracted from :meth:`provision` so the platform can mint an agent's Entra
        identity BEFORE its AgentCore runtime exists (E20 pre-registration). Touches NO
        runtime boto3 (no ``get_agent_runtime`` / ``update_agent_runtime``), so it is safe
        when ``agent.agent_arn`` is ``None``. Leaves ``identity_status='pending'`` (the
        flip to ``'provisioned'`` happens in :meth:`provision_runtime`, once the runtime
        authorizer is wired). Individually idempotent + resumable (create is skipped when
        ``entra_sp_id`` is set); raises on failure (the caller — :meth:`provision` — owns
        the persist-'failed' envelope).
        """
        # (1) Create the per-agent Entra app reg + SP — UNLESS the agent already
        # carries an entra_sp_id (a re-provision: skip create, the skip-guard the
        # CRITIQUE-FIX-A persist exists to protect).
        if not agent.entra_sp_id:
            created = await self._graph.create_agent_app(agent.id, agent.name)
            agent.entra_app_id = created["app_id"]
            agent.entra_sp_id = created["sp_id"]
            agent.invoker_role_id = created["invoker_role_id"]
            agent.admin_role_id = created["admin_role_id"]
            # AUDIENCE-FORM — RESOLVED LIVE 2026-08-12 (E29 livefix-6): with
            # ``requestedAccessTokenVersion=2`` the OBO'd per-agent token's ``aud`` IS
            # the client-id GUID, NOT this api:// URI — the earlier claim here was
            # refuted by a live Databricks exchange (400 invalid_grant until the GUID
            # was on the policy). The split that follows: ``entra_app_audience`` (the
            # URI) stays AgentCore's allowedAudience contract, proven working live —
            # while the Databricks federation half appends ``entra_app_id`` (the GUID),
            # the form the exchange actually matches. Cross-agent distinctness holds
            # for both forms.
            agent.entra_app_audience = created["app_uri"]
            # ``invoke_scope_id`` (the 6th key from create_agent_app) is deliberately
            # NOT persisted: the OBO consent + grants key on the scope/role VALUE
            # ("Invoke"/"Invoker"/"Admin"), not on the scope id. Persist it only if a
            # future task needs id-based scope revocation.
            agent.identity_status = IdentityStatus.PENDING
            # FIX 1 — guard the GET-or-create branch: graph_service's
            # _resolve_existing_app returns sp_id=None if the SP lookup comes back
            # empty (app exists from a prior partial run, SP not yet created). Without
            # this guard we'd persist entra_sp_id=None with status 'pending', then call
            # set_assignment_required(None)/grant_backend_obo_consent(None) against a
            # malformed /servicePrincipals/None path — AND the falsy entra_sp_id would
            # make the NEXT re-provision re-enter create_agent_app → 409 on the
            # duplicate identifierUri (the exact permanent-failure loop CRITIQUE-FIX-A
            # prevents, one level down). The APP ids we DID get are set above so the
            # duplicate-resolve works next time; raising here routes through the
            # failure path (persist 'failed', raise) WITHOUT advancing to steps 2-4.
            if not agent.entra_sp_id:
                raise ProvisioningError(
                    f"create_agent_app returned no sp_id for agent {agent.id}"
                )
            # CRITIQUE-FIX-A: persist the ids IMMEDIATELY, before steps 2-4. The
            # sequence is non-atomic — if a later step fails and we hadn't persisted
            # first, the failure-persist would write entra_sp_id=None, the
            # re-provision skip-guard would re-create the app, and the duplicate
            # identifierUri would 409 forever.
            self._registry.persist_identity(agent)

        # (2) Require an app-role assignment to mint a token for the app.
        await self._graph.set_assignment_required(agent.entra_sp_id)

        # (3) Wire backend→agent delegated OBO consent (idempotent).
        await self._graph.grant_backend_obo_consent(agent.entra_sp_id)

        return agent

    async def provision_runtime(self, agent: Agent) -> Agent:
        """Wire EVERY runtime's inbound JWT authorizer + flip to 'provisioned' (post-deploy).

        Extracted from :meth:`provision` (step 4 + the final persist). Requires at least one
        runtime to exist — raises :class:`ValueError` when the agent names none (E20 calls this
        only AFTER the runtime is deployed and its ARN is known). Assumes the Entra identity was
        already minted (``entra_app_audience`` / ``entra_app_id`` set by
        :meth:`provision_identity`). Raises on failure (the caller — :meth:`provision` — owns
        the persist-'failed' envelope).

        EVERY RUNTIME, NOT JUST ONE (E28A/T1, D-A4 defect 3). Since T1b an agent owns one
        runtime per stage. A runtime with no ``authorizerConfiguration`` accepts UNAUTHENTICATED
        invocations, so configuring only the stored scalar leaves the other runtime **born
        unauthorized** — an ungoverned copy of a governed agent. The Entra identity is minted
        per AGENT, so there is no per-stage question to answer here: all of them get it.
        """
        if not agent.runtime_arns():
            raise ValueError("provision_runtime requires agent.agent_arn or agent.agent_arns")

        # (4) Configure each runtime's inbound JWT authorizer. This is SYNC blocking
        # boto3 with a ~minute+ poll-to-READY PER RUNTIME; provision() runs on the single
        # uvicorn event loop (via BackgroundTasks), so dispatch it OFF the loop
        # (CRITIQUE-FIX-B) to avoid freezing health checks. Dispatched ONCE for the whole
        # fan-out rather than per runtime — the loop belongs off-thread either way, and one
        # hand-off keeps the ordering deterministic.
        await anyio.to_thread.run_sync(self._configure_runtime_authorizers, agent)

        # (5) Done.
        agent.identity_status = IdentityStatus.PROVISIONED
        self._registry.persist_identity(agent)
        return agent

    # -- teardown (Epic 23, Task T2) — the inverse of provision ------------

    def _credential_service(self):
        """The :class:`AgentCredentialService` for the OBO-provider teardown, built on first
        use from THIS service's own Graph client, control client and region.

        Lazy + self-built so :meth:`delete_identity` gained a collaborator without a wiring
        change at any of its call sites (the routes' identity singleton, the repo cascade's
        injected service, and every existing test). The provider name is derived from the
        agent id alone, so nothing about this instance's configuration can make the deleter
        address the wrong provider."""
        if self._credentials is None:
            from services.agent_credential_service import AgentCredentialService

            self._credentials = AgentCredentialService(
                graph=self._graph, control_client=self._control, region=self._region
            )
        return self._credentials

    async def delete_obo_provider(self, agent_id: str) -> None:
        """Delete the agent's ``agp-agent-obo-{id}`` Token Vault entry, propagating failure.

        The un-swallowed half of :meth:`delete_identity`'s provider leg, for a cascade that
        reports the vault entry as its OWN line-item (E36/T16 fix round 1). Idempotent — the
        deleter swallows not-found — so it is safe to call for an agent that was never granted
        an MCP. Nothing here needs the ``Agent``: the provider name derives from the id alone,
        so a half-gone envelope still resolves."""
        await self._credential_service().delete_agent_obo_provider(agent_id)

    async def delete_identity(self, agent: Agent, *, include_obo_provider: bool = True) -> None:
        """Tear down the agent's Entra identity (app reg + SP) and its OBO credential
        provider.

        The inverse of :meth:`provision_identity`'s step 1. Delegates to
        ``GraphService.delete_agent_app`` (T1), which is idempotent — it swallows a
        404 and no-ops on blank ids — so this is safe to call unconditionally from the
        repo-teardown cascade (an agent that never got an identity carries blank ids).

        E36/T16 (research item 5B) adds the AgentCore Token Vault half, FIRST: the per-agent
        ``MicrosoftOauth2`` credential provider ``agp-agent-obo-{id}``, created get-or-create
        at MCP-grant time and deleted by nothing. The Entra app delete cascades the client
        secret Entra holds, but the vault entry survived it — holding a dangling
        clientId/clientSecret for an application that no longer exists, one per agent ever
        granted an MCP. Ordered BEFORE the app delete because the app is the thing whose
        disappearance makes the entry dangling.

        The bundled provider leg's failure is SWALLOWED into a warning (the deleter itself only
        swallows not-found), and that asymmetry with the Entra leg is deliberate: in the
        repo cascade the ``identity`` line-item is BLOCKING
        (``project_service._NON_BLOCKING_ITEMS`` holds only ``langfuse`` and ``exec_role``),
        so a raise here would flip an already-deleted Entra app to ``failed`` and trap the
        registry row behind a vault entry no retry of that cascade can fix. The deletion is
        idempotent, so the entry is still reclaimed by the next teardown attempt.

        ``include_obo_provider=False`` opts out of the bundled leg (E36/T16 fix round 1). It
        exists for a cascade where NOTHING is blocking — ``DELETE /agents/{id}``'s
        registered-external path — because there the swallow would let the route report
        ``identity: deleted`` while the Token Vault entry survived, exactly the false
        ``deleted`` the epic exists to remove. That caller runs :meth:`delete_obo_provider` as
        its own ``obo_provider`` line-item instead, so the failure is visible and no second
        delete call is issued. The default keeps the repo cascade byte-identical.
        """
        if include_obo_provider:
            try:
                await self._credential_service().delete_agent_obo_provider(agent.id)
            except Exception:  # noqa: BLE001 — must not block the (blocking) identity item
                logger.warning(
                    "[agent_identity] OBO credential provider teardown best-effort no-op for "
                    "agent %s (the Token Vault entry may survive; a re-run reclaims it)",
                    agent.id,
                    exc_info=True,
                )
        await self._graph.delete_agent_app(
            entra_app_id=agent.entra_app_id, entra_sp_id=agent.entra_sp_id
        )

    def delete_runtime(self, agent_arn: str, *, control_client=None) -> None:
        """SYNC blocking boto3: delete the agent's AgentCore Runtime.

        The inverse of the runtime half. A SYNC sibling of :meth:`set_runtime_environment`
        (``RID``-not-ARN: DeleteAgentRuntime takes ``agentRuntimeId`` — the ARN's last
        ``/``-segment). Idempotent for the repo-teardown cascade: a falsy ``agent_arn``
        means nothing was provisioned → return immediately.

        E23/T11 hardening: PROBE with ``get_agent_runtime`` FIRST, but skip the delete
        ONLY on a GENUINE ``ResourceNotFoundException`` (the runtime is definitively gone).
        AccessDenied is AMBIGUOUS — a LIVE runtime behind an IAM/SCP/wrong-region misconfig
        returns it too — so on AccessDenied (or any non-NotFound probe error) we do NOT
        infer gone: we ATTEMPT the delete. With the broad ``bedrock-agentcore:*`` grant a
        real gone-runtime probes cleanly (or NotFound); a genuine AccessDenied is a real
        problem that must surface as a FAILED teardown step (record kept for retry), NOT a
        silent skip-as-success that would orphan a live runtime + lose its ARN forever. The
        not-found ``ClientError`` swallow on the delete call stays as the idempotency
        backstop (a delete that races to NotFound is fine); any other ``ClientError``
        propagates (fail loud). Dispatched OFF the event loop by the caller (sync boto3).

        E36/T8 — ``control_client``: the client to run BOTH the probe and the delete under,
        defaulting to this service's ambient one. A tenant stage carrying a
        ``deploy_role_arn`` puts its runtime in the TENANT's account, which the ambient
        control-plane credentials cannot see at all; the caller resolves a per-stage client
        via ``services.tenant_credentials.stage_client`` and passes it here. THE PROBE IS THE
        LOAD-BEARING HALF: under ambient credentials ``get_agent_runtime`` answers a genuine
        ``ResourceNotFoundException`` about an account that never held the runtime, this
        method returns early, and the cascade records ``deleted`` on a live, billing runtime.
        Threading the client into the delete alone would change nothing.
        """
        if not agent_arn:
            return
        client = control_client or self._control
        # Probe first: skip the delete ONLY when the runtime is DEFINITIVELY gone
        # (ResourceNotFoundException). runtime_exists returns False on genuine NotFound and
        # RAISES on AccessDenied/other — the raise falls through to the delete ATTEMPT
        # below (AccessDenied is ambiguous, never inferred as gone). SAME client as the
        # delete: a probe in a different account is the E36/T8 defect by construction.
        try:
            if not self.runtime_exists(agent_arn, control_client=client):
                return
        except ClientError as err:
            if _is_not_found(err):
                return
            # Ambiguous (AccessDenied/other): do NOT skip — fall through to attempt delete.
        runtime_id = agent_arn.rsplit("/", 1)[-1]
        try:
            client.delete_agent_runtime(agentRuntimeId=runtime_id)
        except ClientError as err:
            if _is_not_found(err):
                return
            raise

    def runtime_exists(self, agent_arn: str, *, control_client=None) -> bool:
        """SYNC boto3 reachability probe: does the AgentCore Runtime still exist? (E23/T11).

        The read-only probe the delete-preview endpoint (and :meth:`delete_runtime`) use.
        A falsy ``agent_arn`` (nothing provisioned) → False. A successful
        ``get_agent_runtime`` → True. A GENUINE ``ResourceNotFoundException`` → False
        (definitively gone). Any OTHER ``ClientError`` — including the AMBIGUOUS AccessDenied
        (a live runtime behind an IAM/SCP/region misconfig returns it too) — PROPAGATES:
        this three-way (present / notfound / ambiguous) is intentional so the caller maps
        NotFound→"gone" (confident) and a raised ambiguous error→"unknown" (offer it,
        checked). AccessDenied is NEVER inferred as gone — a possibly-live runtime must not
        be shown "gone".

        E36/T8 — ``control_client`` (defaults to the ambient one) is the account this probe
        asks. A NotFound is only trustworthy from the account that OWNS the runtime: asked
        with control-plane credentials about a tenant-account runtime it means "not here",
        which is not the same fact as "gone" and must never be reported as one.
        """
        if not agent_arn:
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

    def runtime_status(
        self, agent: Agent, stage: str | None = None
    ) -> RuntimeStatusWithEnv:
        """SYNC boto3: read one agent's live AgentCore Runtime status (E28/T5, D9/C2).

        The read-only sibling of :meth:`runtime_exists`, and the ONLY producer of
        :class:`RuntimeStatus`. `runtime_exists` answers a yes/no for the delete path and
        RAISES on an ambiguous error so the caller can decide; this answers "what is it
        doing?" for a UI and therefore NEVER raises — a read surface that 5xx'd on a
        throttle would leave the fleet view blank, which is the same "no answer" the epic
        is fixing. Every failure degrades to a status value with a hint.

        The three-way `runtime_exists` documents is what the mapping is built on:

          - a genuine ``ResourceNotFoundException`` → ``not_deployed`` (definitively gone);
          - any OTHER ``ClientError`` — including the AMBIGUOUS AccessDenied, which a LIVE
            runtime behind an IAM/SCP/wrong-region misconfig also returns — → ``unknown``;
          - a transport/config error (``BotoCoreError``: endpoint unreachable, no creds) →
            ``unknown``.

        ``unknown`` is NEVER ``failed``. That distinction is the point of the route: a
        governance product that renders a probe failure as a runtime failure states a
        conclusion its evidence does not support, and someone pages an on-call for a
        healthy agent.

        No runtime at all → ``not_deployed`` WITHOUT an AWS call: nothing was ever provisioned
        (the E20 pre-registration state), which is a fact we already hold locally.

        WHICH RUNTIME, AND WHAT ``stage`` MEANS (E28A/T1 — this is the function the pre-E28A
        comment predicted would change). Since T1b an agent owns one runtime PER STAGE and the
        record names them in ``agent_arns``:

          - ``stage=None`` (the default, and the pre-E28A behaviour) probes the runtime the
            ``agent_arn`` scalar names — "whichever stage deployed last" (C-A2 has the buildspec
            write the scalar alongside the map entry). The difference is that we can now REPORT
            which stage that was, by finding the map key holding the same ARN.
          - ``stage="dev"`` probes THAT stage's runtime. A stage the agent has no runtime for is
            ``not_deployed`` with no AWS call — never another stage's runtime, which would answer
            a question the caller did not ask while looking like an answer to the one they did.
          - A LEGACY scalar-only record still reports :data:`UNKNOWN_STAGE`, because it genuinely
            cannot attribute its one runtime. Asked for an explicit stage it returns
            ``not_deployed`` rather than captioning the scalar's runtime with that stage.

        THE STAGE VALUE IS EVIDENCE, NOT DECORATION. The frontend's ``runtimeScope`` treats any
        stage other than ``unknown`` as proof the reading is attributable and will caption a
        per-stage pill with it — so a guessed stage would manufacture per-stage evidence out of
        an agent-level fact, the exact error that module exists to make unreachable. ``status``
        and ``stage`` stay INDEPENDENT: an unreachable control plane makes the status unknown,
        but which runtime we asked about is still known from the record.

        TWO PRODUCERS SINCE E29/T10, dispatched by platform on the SAME chain the provisioning
        and invoke seams use. A Databricks-governed agent is answered by the Databricks producer
        (an app listing, not boto3) and returns immediately; EVERYTHING ELSE falls through to
        the body below, byte for byte as before. Written as an early return with NO ``elif``
        arm: a metadata-only record (no ARN, no handle) passes NEITHER gate and must still get
        today's local ``not_deployed`` — an if/elif chain would answer it with ``None``.

        SYNC blocking boto3 — the caller dispatches it OFF the event loop.

        E36/T12 — the result is a :class:`RuntimeStatusWithEnv`: the same status contract, plus
        the runtime's live ``environmentVariables``, which this read already fetched and used to
        throw away. That env is an INTERNAL hand-off to ``reconcile_runtime_mcp_env`` and is
        never serialized (the route's ``response_model`` is the plain ``RuntimeStatus``). It
        follows the same evidence discipline as ``status``: a read that REACHED the runtime
        carries a dict (``{}`` when the runtime holds no env), and EVERY degraded path above —
        ``not_deployed`` and both ``unknown`` branches — carries ``None``, meaning "no evidence".
        """
        if is_databricks_governed_agent(agent):
            return self._databricks_runtime_status(agent, stage)

        checked_at = datetime.now(timezone.utc).isoformat()
        arns = agent.runtime_arns()

        if stage is None:
            # The scalar's runtime (pre-E28A behaviour), now attributed where possible. For a
            # legacy record ``runtime_arns`` already keys its single entry UNKNOWN_STAGE.
            arn = agent.agent_arn or next(iter(arns.values()), None)
            resolved_stage = next(
                (s for s, a in arns.items() if a == arn), UNKNOWN_STAGE
            )
        else:
            arn = arns.get(stage)
            resolved_stage = stage

        def _result(status: str, **extra) -> RuntimeStatusWithEnv:
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

        # RID-not-ARN (research §1): the control call takes the ARN's last "/"-segment.
        runtime_id = arn.rsplit("/", 1)[-1]
        try:
            resp = self._control.get_agent_runtime(agentRuntimeId=runtime_id)
        except ClientError as err:
            if _is_not_found(err):
                return _result(
                    "not_deployed", detail="the runtime no longer exists"
                )
            return _result("unknown", detail=_safe_probe_detail(err))
        except BotoCoreError:
            # Transport/config (endpoint unreachable, no credentials). `str(err)` can embed
            # an endpoint or profile, so it is never surfaced — only logged.
            logger.warning(
                "[runtime-status] control-plane read failed for agent %s", agent.id,
                exc_info=True,
            )
            return _result("unknown", detail=_PROBE_UNREACHABLE_DETAIL)

        native = resp.get("status")
        status = _NATIVE_TO_RUNTIME_STATUS.get(native, "unknown")
        detail = None
        if status == "unknown":
            # Either a deliberate DELETING, or a status this platform version does not
            # know. Naming the native value is safe (it is a fixed AgentCore vocabulary,
            # not caller-supplied) and is what makes the state debuggable.
            detail = (
                f"runtime is {native.lower()}"
                if isinstance(native, str) and _SAFE_ERROR_CODE.match(native.replace("_", ""))
                else "the runtime reported no recognizable status"
            )
        # E36/T12: the live env — the ONLY read that holds it — travels with the status so the
        # caller can reconcile a wiped MCP wiring without a second AWS call. Carried on the
        # subclass and NEVER serialized (see :class:`RuntimeStatusWithEnv`).
        #
        # THIS read reached the runtime, so whatever it says about the env is EVIDENCE and must
        # arrive as a DICT. A runtime carrying no ``environmentVariables`` at all reads as ``{}``
        # — "the env is empty", which is precisely what a wipe looks like — never as ``None``,
        # which every failing path above uses for "this read holds NO evidence about the env".
        # Collapsing the two made the reconciler treat a throttle or an AccessDenied as a wipe.
        live_env = resp.get("environmentVariables")
        return _result(
            status,
            detail=detail,
            image_tag=_image_tag(resp),
            environment_variables=live_env if isinstance(live_env, dict) else {},
        )

    def _databricks_runtime_status(
        self, agent: Agent, stage: str | None
    ) -> RuntimeStatusWithEnv:
        """Delegate to the Databricks producer, and NEVER raise (E29/T10).

        The collaborator promises the same never-raises contract, but this is the method the
        route calls, so the guarantee is enforced HERE too — a 5xx from a status read blanks the
        fleet view, and defense in depth is cheap when the fallback value is honest.

        A missing collaborator is ``unknown``, NOT ``not_deployed``. The latter is a positive
        claim that nothing is running; the truth is that nothing was wired to ask. That is the
        same conflation ``unknown`` exists to prevent, one layer up. (It is also why the
        provisioning seam RAISES for a missing collaborator while this one degrades: provisioning
        is a write that must not silently no-op, this is a read that must not 500.)

        RETURNS the E36/T12 carrier, ALWAYS with ``environment_variables=None``. The route
        dereferences that field unguarded on every producer path, and ``None`` is also the true
        evidence claim here: a Databricks runtime holds no backend-injected MCP env to observe,
        so there is never anything for ``reconcile_runtime_mcp_env`` to heal — the ``None``
        makes it a structural no-op, not a suppressed one. The collaborator keeps returning the
        plain wire model; the re-wrap happens at this seam so the internal carrier stays this
        module's private concern.
        """
        checked_at = datetime.now(timezone.utc).isoformat()

        def _degraded(detail: str) -> RuntimeStatusWithEnv:
            return RuntimeStatusWithEnv(
                agent_id=agent.id,
                stage=stage or UNKNOWN_STAGE,
                status="unknown",
                checked_at=checked_at,
                detail=detail,
            )

        if self._databricks_identity is None:
            return _degraded(
                "runtime status could not be read (no Databricks reader is configured)"
            )
        try:
            status = self._databricks_identity.runtime_status(agent, stage=stage)
        except Exception:  # noqa: BLE001 — a read surface has no honest way to raise
            logger.warning(
                "[runtime-status] Databricks read failed for agent %s",
                agent.id,
                exc_info=True,
            )
            return _degraded(_PROBE_UNREACHABLE_DETAIL)
        return RuntimeStatusWithEnv(**status.model_dump())

    def _configure_runtime_authorizers(self, agent: Agent) -> None:
        """SYNC: wire the inbound JWT authorizer on EVERY runtime the agent owns (E28A/T1).

        The fan-out over :meth:`_configure_runtime_authorizer`, which stays single-runtime —
        that is the mechanics, this is the policy. Dispatched OFF the event loop by
        :meth:`provision_runtime` (one hand-off for the whole fan-out).

        ATTEMPT-ALL-THEN-RAISE. The runtimes are independent, so one unreachable runtime must
        not stop the others from being authorized — a partial application strictly beats
        abandoning the rest, exactly like ``project_service._delete_runtime_state``'s per-key
        tolerance. But this DOES raise at the end, because the caller flips the agent to
        ``provisioned`` on success and an agent reported provisioned while one of its runtimes
        accepts UNAUTHENTICATED invocations is the dangerous outcome, not the noisy one. Each
        failure is logged with its stage so every ungoverned runtime is nameable rather than
        hidden behind one "the loop died" line."""
        arns = agent.runtime_arns()
        failed: list[str] = []
        for stage, arn in arns.items():
            try:
                self._configure_runtime_authorizer(
                    arn, agent.entra_app_audience, agent.entra_app_id
                )
            except Exception:  # noqa: BLE001 — per-runtime tolerance; re-raised below
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

    # -- runtime authorizer (sync, off-loop) -------------------------------

    def _configure_runtime_authorizer(
        self, agent_arn: str, agent_audience: str, agent_client_id: str | None = None
    ) -> None:
        """SYNC blocking boto3: set the runtime's inbound JWT authorizer (research §1).

        ``RID``-not-ARN (research §1): the control calls take ``agentRuntimeId`` — the
        ARN's last ``/``-segment; passing the ARN yields a misleading
        ``AccessDeniedException``. UpdateAgentRuntime is a FULL-REPLACE, so this
        ``get_agent_runtime`` → replays ``agentRuntimeArtifact`` / ``roleArn`` /
        ``networkConfiguration`` + adds ``authorizerConfiguration`` → then polls
        ``get_agent_runtime`` to ``READY`` (bounded loop + sleep).
        """
        runtime_id = agent_arn.rsplit("/", 1)[-1]

        current = self._control.get_agent_runtime(agentRuntimeId=runtime_id)

        authorizer_configuration = {
            "customJWTAuthorizer": {
                "discoveryUrl": (
                    f"{self._login_base}/{self._tenant_id}"
                    "/v2.0/.well-known/openid-configuration"
                ),
                # Audience ONLY (Decision 3) — no allowedClients / customClaims.
                # AUDIENCE-FORM (live, 2026-06-03): the OBO'd token's real ``aud`` can be
                # the per-agent app's CLIENT GUID rather than the ``api://agp-agent-<id>``
                # URI form. Accept BOTH (each is per-agent-unique; only Entra mints them
                # for an assigned principal, so cross-agent-replay safety holds) so the
                # runtime matches whichever form Entra emits. The OBO scope still uses the
                # URI (``{audience}/.default``) — scope ≠ aud.
                "allowedAudience": [
                    a for a in (agent_audience, agent_client_id) if a
                ],
            }
        }

        # Full-replace: replay the three required fields from the GET + add the authorizer.
        self._control.update_agent_runtime(
            agentRuntimeId=runtime_id,
            agentRuntimeArtifact=current["agentRuntimeArtifact"],
            roleArn=current["roleArn"],
            networkConfiguration=current["networkConfiguration"],
            authorizerConfiguration=authorizer_configuration,
        )

        self._poll_runtime_ready(runtime_id)

    # -- runtime environment (sync, off-loop) — Epic 7, Tier-2 -------------

    def set_runtime_environment(
        self,
        agent_arn: str,
        env: dict[str, str],
        *,
        wait_ready: bool = True,
        control_client=None,
    ) -> str:
        """SYNC blocking boto3: MERGE container env vars onto an existing AgentCore Runtime.

        Epic 7, Tier-2 (the env-injection seam): the agent reads its MCP config from
        ``os.environ``; nothing sets that on its runtime, so it ``KeyError``s on invoke. The
        platform injects it at GRANT time (the first moment all values exist). This function
        is env-AGNOSTIC — a generic MERGE + full-replace primitive; WHAT it is called with is
        owned by the caller. Under E12 the caller (``agent_mcp_env.rebuild_runtime_mcp_env``)
        passes the multi-MCP ``MCP_SERVERS`` map (a JSON list of granted MCPs) +
        ``CREDENTIAL_PROVIDER_NAME``, with the legacy single-MCP ``MCP_AUDIENCE`` /
        ``MCP_GATEWAY_URL`` keys neutralized to ``""`` (so a stale single value can't linger
        alongside the list). This is a SIBLING of :meth:`_configure_runtime_authorizer` (the
        env twin of the authorizer set), mirroring its mechanics exactly:

          - ``RID``-not-ARN: the control calls take ``agentRuntimeId`` (the ARN's last
            ``/``-segment); passing the ARN yields a misleading ``AccessDeniedException``.
          - ``UpdateAgentRuntime`` is a FULL-REPLACE PUT (research §12.7): a field NOT replayed
            is DROPPED. So this GETs the runtime and replays ALL required fields
            (``agentRuntimeArtifact`` / ``roleArn`` / ``networkConfiguration``) AND — critically —
            the inbound ``authorizerConfiguration`` / ``authorizerType`` (the E6 security gate:
            dropping it would silently reopen the agent's gate) AND any optional fields present
            (``protocolConfiguration`` / ``requestHeaderConfiguration`` / ``lifecycleConfiguration``
            / ``metadataConfiguration`` / ``filesystemConfigurations`` / ``description``), then
            polls ``get_agent_runtime`` to ``READY``.
          - The env vars carry on the top-level ``environmentVariables`` map (botocore 1.43.16:
            ``UpdateAgentRuntime`` ``EnvironmentVariablesMap``), NOT nested in the artifact.

        MERGE (existing ∪ new; new keys win): the runtime's current ``environmentVariables`` are
        read from the GET and the injected ``env`` is layered on top, so we never clobber env the
        runtime already needs. Idempotent — re-applying the same ``env`` converges. Raises a
        ``RuntimeError`` on a terminal/``*_FAILED`` status or poll timeout (same type
        :meth:`_poll_runtime_ready` raises for the authorizer path); the caller (the grant route)
        converts it to a fail-loud 5xx.

        Dispatched OFF the event loop by the caller (research §12.3 — sync boto3 with a
        ~minute+ poll must not freeze the uvicorn loop).

        ``wait_ready`` (E36/T12, fix round 1) — DEFAULT True, so the GRANT/REVOKE path is byte
        for byte what it was: that caller reports a governance fact back to an operator, so it
        must not claim a wiring it has not seen converge, and a ``*_FAILED`` runtime has to reach
        it as a 502. Passed False by the RECONCILE-ON-READ path and by the register-time Langfuse
        wiring (E36/T13, fix round 1) — both surfaces where the poll is pure
        cost: :meth:`_poll_runtime_ready` ``time.sleep``s up to
        ``_POLL_MAX_ATTEMPTS × _POLL_INTERVAL_SECONDS`` (300 s) PER runtime, and that sleep would
        be spent holding one of the ~40 shared ``anyio.to_thread`` workers every sync boto3 call
        in the backend competes for — neither a read surface nor an unconditional per-registration
        background hook may park a thread for minutes. Not waiting also cannot hide a failure: an
        update that never converges leaves the injected keys absent from the runtime, so the NEXT
        read (or the next wiring run) observes exactly that and re-triggers. Nothing downstream of
        the accepted ``UpdateAgentRuntime`` is skipped except the observation.

        ``control_client`` (E36/T13) — the client to run the GET, the UPDATE **and** the poll
        under, defaulting to this service's ambient one. Same keyword, same meaning and the same
        ``client = control_client or self._control`` line as :meth:`delete_runtime` /
        :meth:`runtime_exists`, and it exists for the ceiling the class docstring names above: a
        tenant stage carrying a ``deploy_role_arn`` puts its runtime in the TENANT's account,
        where the ambient control-plane credentials see nothing at all. ALL THREE calls must use
        it — a GET in one account and an UPDATE in another cannot be made to mean anything, and
        a poll in the wrong account answers ``ResourceNotFoundException`` forever, i.e. a
        successful write reported as a timeout. The caller resolves it via
        ``services.tenant_credentials.stage_client``.

        RETURNS the runtime's execution ``roleArn`` (E36/T13) — read from the GET this method
        already performs and previously discarded. The Langfuse wiring has to grant that exact
        principal ``secretsmanager:GetSecretValue`` on the per-agent secret, and returning the
        value it already holds is what keeps that from costing a second ``get_agent_runtime``.
        Additive: every pre-T13 caller ignores the return, and the field is required on the
        response (it is replayed into the full-replace below, so a missing one would already
        ``KeyError`` here).
        """
        runtime_id = agent_arn.rsplit("/", 1)[-1]
        client = control_client or self._control

        current = client.get_agent_runtime(agentRuntimeId=runtime_id)

        # MERGE: existing env (if any) ∪ injected; injected keys win. Never clobbers env the
        # runtime already needs (e.g. an operator-set LOG_LEVEL/MODEL_ID).
        merged_env = {**(current.get("environmentVariables") or {}), **env}

        # Full-replace: replay the required fields, then the merged env. Replay the inbound
        # authorizer (the E6 gate — §12.7, NEVER drop it) + any optional fields ONLY if present
        # (mirrors mcp_identity_service's runtime optional-field replay) so the Update doesn't
        # silently drop authorizer/protocol/etc. that were set at create/authorizer-config time.
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
        """Poll ``get_agent_runtime`` until status READY (bounded loop + sleep).

        A ``*_FAILED`` status, or exhausting ``_POLL_MAX_ATTEMPTS``, raises so the
        caller marks the agent ``failed``. ``time.sleep`` is patched to a no-op in tests.

        ``client`` (E36/T13) — poll under the SAME client that wrote, defaulting to the ambient
        one. A cross-account write polled ambiently never observes the runtime at all.
        """
        control = client or self._control
        for _ in range(_POLL_MAX_ATTEMPTS):
            resp = control.get_agent_runtime(agentRuntimeId=runtime_id)
            status = resp.get("status")
            if status == _READY_STATUS:
                return
            if status in _FAILED_STATUSES:
                raise RuntimeError(
                    f"runtime {runtime_id} reached terminal status {status}"
                )
            time.sleep(_POLL_INTERVAL_SECONDS)
        raise RuntimeError(
            f"runtime {runtime_id} did not reach READY within "
            f"{_POLL_MAX_ATTEMPTS} polls"
        )
