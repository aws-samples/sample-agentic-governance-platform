"""Agent Registry CRUD, lifecycle, identity, invoke and per-agent read routes.

Backed by ``AgentRegistryService``. Every detail, mutation, lifecycle, invoke and identity route
loads the agent through ``_load_visible_agent``, so a missing agent and a foreign tenant's agent
answer the same 404. Reads are tenant-gated; the four envelope mutations (``PUT``, ``DELETE``,
``/reprovision``, ``/publish``) also require MAINTAINER on the agent's owning project when the
agent carries a ``project_id``; the lifecycle routes keep their platform roles alone.
"""

import functools
import json
import logging
from datetime import date, timedelta
from typing import Any, List, Optional

import anyio.to_thread
from fastapi import APIRouter, BackgroundTasks, Query, Request
from fastapi import Depends as RBACDepends
from pydantic import Field

from core.config import settings
from core.rbac import Principal, Role, current_principal, require_role
from models.agent import (
    AWS_ARN_REGION_RE,
    GOVERNABLE_PLATFORMS,
    Agent,
    AgentCreate,
    AgentUpdate,
    IdentityStatus,
    LifecycleState,
    Platform,
    RuntimeStatus,
    is_databricks_governed_agent,
    is_inventory_only,
)
from models.base import WireModel
from models.deployment import Deployment
from models.eval import EvalRuns
from models.project_role import ProjectRole
from models.repository import AgentRepositoryView, RepoDeleteItemResult
from models.tenant import TenantPlatform
from models.transition import TransitionRequest
from services.agent_identity_service import (
    AgentIdentityService,
    ProvisioningError,
    is_agentcore_agent,
)
from services.agent_mcp_env import reconcile_runtime_mcp_env
from services.agent_registry_service import (
    AgentNotFoundError,
    AgentRegistryService,
    AgentRequestError,
    apply_creator_sponsor,
)
from services.graph_service import GraphService
from services.langfuse_metrics_service import AgentMetrics, TracePage
from services.langfuse_provisioning import LangfuseProvisioningService
from services.platform_adapter import AdapterError, ArtifactPolicy, InvokeError, PlatformAdapter
from services.platform_adapter_factory import adapter_for, build_adapters
from services.platform_registry import declaration_for
from services.project_errors import ProjectError
from services.project_resolver import ProjectContext
from services.tenant_credentials import StageUnresolvedError, TenantCredentialsError
from services.tenant_resolver import TenantContext, require_member, visible

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/agents", tags=["agents"])

_svc: Optional[AgentRegistryService] = None
_identity_svc: Optional[AgentIdentityService] = None
_databricks_identity_svc = None
_databricks_workspace_svc = None
_langfuse_svc: Optional[LangfuseProvisioningService] = None


def get_service() -> AgentRegistryService:
    """The ``AgentRegistryService`` singleton. The registry is addressed by name and resolved to its
    id on first use (the registry writer's ``registry_id``); ``AGENT_REGISTRY_ID`` is an explicit override
    that skips the lookup.
    """
    global _svc
    if _svc is None:
        _svc = AgentRegistryService(
            registry_id=getattr(settings, "AGENT_REGISTRY_ID", ""),
            registry_name=getattr(settings, "AGENT_REGISTRY_NAME", ""),
            region=getattr(settings, "AGENT_REGISTRY_REGION", "us-east-1"),
        )
    return _svc


def get_graph_service() -> GraphService:
    """The one ``GraphService`` singleton, owned by ``routes/grants.py``; tests patch it there."""
    from api.routes.grants import get_graph_service as _grants_get_graph_service

    return _grants_get_graph_service()


def get_databricks_identity_service():
    """The ``DatabricksIdentityService`` singleton, or ``None`` when no tenants table is configured
    (an AgentCore-only deployment). Imported inside the function: it needs the tenants route's
    singleton, and a module-level import would be a cycle. ``provision`` raises for a Databricks
    agent that arrives with no collaborator, so the gap is a persisted 'failed', never a silent
    skip.
    """
    global _databricks_identity_svc
    if _databricks_identity_svc is None and settings.TENANTS_TABLE_NAME:
        from api.routes.tenants import get_tenant_service
        from services.databricks_identity_service import DatabricksIdentityService
        from services.databricks_workspace_service import DatabricksWorkspaceService

        _databricks_identity_svc = DatabricksIdentityService(
            databricks=DatabricksWorkspaceService(),
            registry=get_service(),
            tenants=get_tenant_service(),
            secret_prefix=f"{settings.DATABRICKS_TENANT_SECRET_PREFIX}agents/",
            region=settings.AWS_REGION,
        )
    return _databricks_identity_svc


def get_databricks_workspace_service():
    """The stateless ``DatabricksWorkspaceService`` singleton for the invoke path; it holds no
    credential of its own, so it needs no tenants table.
    """
    global _databricks_workspace_svc
    if _databricks_workspace_svc is None:
        from services.databricks_workspace_service import DatabricksWorkspaceService

        _databricks_workspace_svc = DatabricksWorkspaceService()
    return _databricks_workspace_svc


def get_identity_service() -> AgentIdentityService:
    """The ``AgentIdentityService`` singleton: the AgentCore and platform-neutral Entra half of
    provisioning. The platform half is dispatched through ``get_adapters``.
    """
    global _identity_svc
    if _identity_svc is None:
        _identity_svc = AgentIdentityService(
            graph=get_graph_service(),
            registry=get_service(),
            tenant_id=settings.ENTRA_TENANT_ID,
            login_base=settings.ENTRA_LOGIN_BASE,
            region=getattr(settings, "AGENT_REGISTRY_REGION", "") or "us-east-1",
        )
    return _identity_svc


def get_adapters() -> dict:
    """The ``PlatformAdapter`` dict, rebuilt on every call: the adapters are stateless wrappers
    around the singletons above, and a cached dict would pin whichever collaborator existed at
    first call while tests swap them.
    """
    from api.routes.tenants import get_tenant_service

    return build_adapters(
        identity=get_identity_service(),
        databricks_identity=get_databricks_identity_service(),
        tenant_service=get_tenant_service(),
    )


def platform_runtime_binding(agent: Agent):
    """The platform half of provisioning for ``agent``, as ``AgentIdentityService.provision``
    consumes it. A plain function that resolves the adapter when called, inside ``provision``'s
    persist-'failed' envelope, so an unwired platform lands as a persisted 'failed' instead of a
    record stranded at 'pending'.
    """
    adapter = adapter_for(get_adapters(), _governable_platform(agent))
    return adapter.provision_runtime_binding(agent)


def _governable_platform(agent: Agent) -> Optional[TenantPlatform]:
    return GOVERNABLE_PLATFORMS.get(agent.platform) if agent.platform is not None else None


def status_adapter(agent: Agent) -> PlatformAdapter:
    """The adapter that answers a status read for ``agent``; never raises. A governable platform with
    no adapter wired degrades to that platform's collaborator-less adapter, whose own read arm owns
    the honest "nothing is wired" text; an inventory-only label reads through the AWS adapter, which
    answers ``not_deployed`` locally.
    """
    platform = _governable_platform(agent) or TenantPlatform.AWS
    try:
        return adapter_for(get_adapters(), platform)
    except AdapterError:
        from services.platform_adapters.databricks import DatabricksPlatformAdapter

        return DatabricksPlatformAdapter(None)


def get_langfuse_service() -> LangfuseProvisioningService:
    """The ``LangfuseProvisioningService`` singleton; the tenants route's singleton is imported
    lazily to avoid a cycle.
    """
    global _langfuse_svc
    if _langfuse_svc is None:
        from api.routes.tenants import get_tenant_service

        _langfuse_svc = LangfuseProvisioningService(
            langfuse_host=settings.LANGFUSE_HOST,
            langfuse_secret_name=settings.LANGFUSE_ADMIN_SECRET_NAME,
            region=getattr(settings, "AGENT_REGISTRY_REGION", "") or "us-east-1",
            registry=get_service(),
            tenants=get_tenant_service(),
        )
    return _langfuse_svc


def provision_langfuse_best_effort(agent: Agent, service: LangfuseProvisioningService) -> None:
    """Register-time hook: provision the agent's Langfuse project and key, then wire its runtime.

    Best effort: every failure is logged and swallowed, so registration never fails on
    observability, and a later run is idempotent. The runtime wiring is skipped when
    ``_auto_trace_delivery`` says the operator chose manual delivery or the platform cannot
    auto-deliver.
    """
    if not settings.LANGFUSE_HOST:
        return
    try:
        joined = service.provision_agent_project(agent)
    except Exception:
        logger.exception(
            "[agents] Langfuse project provisioning failed for agent %s "
            "(non-blocking; fields stay unprovisioned)",
            agent.id,
        )
        return

    secret_name = (joined or {}).get("secret_name")
    if not secret_name:
        return
    if not _auto_trace_delivery(agent):
        logger.info(
            "[agents] Langfuse project provisioned for agent %s but runtime env NOT injected "
            "(trace delivery is manual or the platform cannot auto-deliver — self-serve wiring)",
            agent.id,
        )
        return
    try:
        service.wire_agent_runtime(agent, secret_name, get_identity_service())
    except Exception:
        logger.exception(
            "[agents] Langfuse runtime wiring failed for agent %s (non-blocking; the project "
            "exists but the runtime was not told about it)",
            agent.id,
        )


def _auto_trace_delivery(agent: Agent) -> bool:
    """Whether the OTLP env is injected onto the runtime: the platform must auto-deliver
    (``PlatformDeclaration.trace_auto_delivery``) and the agent's ``trace_delivery`` must not be
    ``"manual"``.
    """
    if agent.trace_delivery == "manual":
        return False
    tenant_platform = _governable_platform(agent)
    if tenant_platform is None:
        return False
    return declaration_for(tenant_platform).trace_auto_delivery


async def get_tenant_ctx(
    principal: Principal = RBACDepends(current_principal),
) -> TenantContext:
    """Delegates to the tenant-resolver singleton ``routes/users.py`` owns, imported lazily to avoid a
    cycle; tests patch it there.
    """
    from api.routes.users import get_tenant_ctx as _users_get_tenant_ctx

    return await _users_get_tenant_ctx(principal)


async def get_project_ctx(
    principal: Principal = RBACDepends(current_principal),
) -> ProjectContext:
    """Delegates to the project-resolver singleton ``routes/users.py`` owns, imported lazily to avoid
    a cycle; tests patch it there.
    """
    from api.routes.users import get_project_ctx as _users_get_project_ctx

    return await _users_get_project_ctx(principal)


def _require_agent_project_role(
    agent: Agent, pctx: ProjectContext, required: ProjectRole, *, principal: Principal
) -> None:
    """Gate a mutation on the caller's role over the agent's owning project; a no-op when the agent
    has no ``project_id``. Runs after ``_load_visible_agent``, so a 403 can only confirm an agent
    the caller's tenant already exposes. Reuses the project routes' gate so both surfaces refuse
    identically, stale-cache refresh included.
    """
    if agent.project_id is None:
        return
    from api.routes.projects import _require_project_role_or_ungoverned

    _require_project_role_or_ungoverned(pctx, agent.project_id, required, principal=principal)


async def _load_visible_agent(agent_id: str, ctx: TenantContext) -> Agent:
    """The agent by id, gated on tenant visibility before any side effect; a missing agent and a
    foreign tenant's agent answer the same 404 "Agent not found".
    """
    agent = get_service().get(agent_id)
    if not agent or not visible(ctx, agent.tenant_id):
        raise AgentNotFoundError(f"agent {agent_id} is not visible")
    return agent


class PublishToggle(WireModel):
    """Body for the cross-tenant publish toggle."""

    published: bool


def _tenant_for_platform_check(platform, tenant_id: Optional[str]):
    """The tenant to judge ``platform`` against, or ``None`` when there is nothing to judge: no
    governable label, or a record carrying no ``tenant_id``. ``TenantService.get`` raises
    ``not_found`` for an unknown tenant and for a store that would not answer, so a governable label
    whose tenant cannot be read is refused with the tenant domain's row, never waved through.
    """
    if platform is None or not tenant_id or is_inventory_only(platform):
        return None
    from api.routes.tenants import get_tenant_service

    return get_tenant_service().get(tenant_id)


def _refuse_platform_tenant_mismatch(platform, tenant) -> None:
    """Refuse a governable agent label that contradicts its tenant's platform (409); ``tenant``
    ``None`` or an inventory-only label means there is nothing to contradict.
    """
    if platform is None or tenant is None or is_inventory_only(platform):
        return
    if GOVERNABLE_PLATFORMS[Platform(platform)] != tenant.platform:
        error = AgentRequestError("platform label contradicts the tenant", kind="platform_tenant_mismatch")
        logger.warning(
            "[agents] refused a platform label under tenant %s (%s)", getattr(tenant, "id", ""), error.code
        )
        raise error


@router.post("", response_model=Agent, status_code=201)
async def create_agent(
    req: AgentCreate,
    background_tasks: BackgroundTasks,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    from api.routes.tenants import get_tenant_service

    tenant = get_tenant_service().get(req.tenant_id)
    require_member(ctx, req.tenant_id)

    _refuse_platform_tenant_mismatch(req.platform, tenant)

    apply_creator_sponsor(req, principal)

    svc = get_service()
    agent = svc.create(req, created_by=principal.email or principal.oid)

    if is_agentcore_agent(agent) or is_databricks_governed_agent(agent):
        background_tasks.add_task(
            get_identity_service().provision,
            agent,
            runtime_binding=platform_runtime_binding,
        )

    background_tasks.add_task(provision_langfuse_best_effort, agent, get_langfuse_service())

    return agent


@router.get("", response_model=List[Agent])
async def list_agents(
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.VIEWER)),
    lifecycle_state: Optional[LifecycleState] = Query(default=None),
    sponsor_oid: Optional[str] = Query(default=None),
    business_unit: Optional[str] = Query(default=None),
    region: Optional[str] = Query(default=None),
    platform: Optional[Platform] = Query(default=None),
):
    svc = get_service()
    records = svc.list(
        lifecycle_state=lifecycle_state,
        sponsor_oid=sponsor_oid,
        business_unit=business_unit,
        region=region,
        platform=platform,
    )
    return [a for a in records if visible(ctx, a.tenant_id)]


@router.get("/{agent_id}", response_model=Agent)
async def get_agent(
    agent_id: str,
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.VIEWER)),
):
    return await _load_visible_agent(agent_id, ctx)


@router.put("/{agent_id}", response_model=Agent)
async def update_agent(
    agent_id: str,
    req: AgentUpdate,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    pctx: ProjectContext = RBACDepends(get_project_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    agent = await _load_visible_agent(agent_id, ctx)
    _require_agent_project_role(agent, pctx, ProjectRole.MAINTAINER, principal=principal)

    effective_platform = req.platform if req.platform is not None else agent.platform
    _refuse_platform_tenant_mismatch(
        effective_platform,
        _tenant_for_platform_check(effective_platform, agent.tenant_id),
    )

    svc = get_service()
    agent = svc.update(agent_id, req)
    if not agent:
        raise AgentNotFoundError(f"agent {agent_id} vanished before the update")
    return agent


async def _teardown_item(item: str, start) -> RepoDeleteItemResult:
    """Run one best-effort teardown leg and map it to a report item: ``deleted``; ``skipped`` with
    the leg's own reason when it returns a non-empty string; or ``failed`` with a safe reason
    (``assume_role_failed: …``, ``stage_unresolved: …``, or the exception's type name, never a
    token or a provider body). ``start`` is a zero-argument callable so resolving a lazy singleton
    happens inside the guard too. ``routes/mcp_servers.py`` carries the same helper.
    """
    try:
        note = await start()
    except TenantCredentialsError as err:
        logger.exception("[teardown] step '%s' could not assume the tenant role", item)
        return RepoDeleteItemResult(item=item, outcome="failed", reason=f"assume_role_failed: {err.message}")
    except StageUnresolvedError as err:
        logger.exception("[teardown] step '%s' could not resolve the owning account", item)
        return RepoDeleteItemResult(item=item, outcome="failed", reason=f"stage_unresolved: {err.message}")
    except Exception as err:
        logger.exception("[teardown] step '%s' failed", item)
        return RepoDeleteItemResult(item=item, outcome="failed", reason=type(err).__name__)
    if isinstance(note, str) and note:
        return RepoDeleteItemResult(item=item, outcome="skipped", reason=note)
    return RepoDeleteItemResult(item=item, outcome="deleted")


async def _owned_by_a_repository(agent_id: str) -> bool:
    """Whether a repository owns ``agent_id``, asked of the repository partition (``project_id`` is
    also blank on older envelopes). Fails closed: an unanswerable question returns True, so no
    teardown leg runs on a possibly repository-owned agent and the delete does not 500.
    """
    from api.routes.projects import get_project_service

    try:
        repo = await anyio.to_thread.run_sync(get_project_service().find_repository_by_agent_id, agent_id)
    except Exception:
        logger.exception(
            "[teardown] could not establish whether a repository owns agent %s — skipping every teardown leg",
            agent_id,
        )
        return True
    return repo is not None


async def _teardown_registered_agent(agent: Agent) -> List[RepoDeleteItemResult]:
    """Tear down what a registered-external agent owns before its record goes: the
    ``agp-agent-obo-{id}`` credential provider, the Entra app with its service principal, and the
    Langfuse project with its secret. Three best-effort legs, each its own report item, none
    blocking the record delete; a repository-owned agent is skipped entirely because the repo
    cascade owns its teardown. The items are logged under one ``[teardown]`` prefix, the audit
    trail for an operator reclaiming a failed leg by hand once the record is gone.
    """
    if agent.project_id is not None or await _owned_by_a_repository(agent.id):
        return [
            RepoDeleteItemResult(item="obo_provider", outcome="skipped"),
            RepoDeleteItemResult(item="identity", outcome="skipped"),
            RepoDeleteItemResult(item="langfuse", outcome="skipped"),
        ]

    items: List[RepoDeleteItemResult] = []

    if agent.oauth2_credential_provider_name:
        items.append(
            await _teardown_item(
                "obo_provider",
                lambda: get_identity_service().delete_obo_provider(agent.id),
            )
        )
    else:
        items.append(RepoDeleteItemResult(item="obo_provider", outcome="skipped"))

    if agent.entra_app_id or agent.entra_sp_id:
        items.append(
            await _teardown_item(
                "identity",
                lambda: get_identity_service().delete_identity(agent, include_obo_provider=False),
            )
        )
    else:
        items.append(RepoDeleteItemResult(item="identity", outcome="skipped"))

    if settings.LANGFUSE_HOST:
        items.append(
            await _teardown_item(
                "langfuse",
                lambda: anyio.to_thread.run_sync(get_langfuse_service().delete_agent_project, agent),
            )
        )
    else:
        items.append(RepoDeleteItemResult(item="langfuse", outcome="skipped"))

    for entry in items:
        logger.info(
            "[teardown] agent=%s item=%s outcome=%s reason=%s",
            agent.id,
            entry.item,
            entry.outcome,
            entry.reason,
        )
    return items


async def _teardown_platform_artifacts(agent: Agent) -> None:
    """Remove the platform residue the record names before the record goes. The route executes
    policies, not platforms: the adapter enumerates the artifacts and each one's ``ArtifactPolicy``
    decides. A blocking artifact that cannot be removed refuses the delete (``AdapterError`` when
    nothing is wired, ``ProvisioningError`` when the removal was attempted and failed) and the
    record survives for a retry; a best-effort failure is logged as residue.
    """
    adapter = status_adapter(agent)
    for artifact in adapter.named_artifacts(agent):
        if artifact.policy is ArtifactPolicy.NEVER_TOUCH:
            logger.info(
                "[teardown] agent=%s item=%s outcome=skipped reason=never_touch",
                agent.id,
                artifact.kind,
            )
            continue
        try:
            await adapter.remove_artifact(agent, artifact)
        except AdapterError:
            if artifact.policy is ArtifactPolicy.BLOCKING:
                raise
            logger.warning(
                "[teardown] agent=%s item=%s outcome=residue reason=unwired",
                agent.id,
                artifact.kind,
            )
        except ProvisioningError as err:
            if artifact.policy is ArtifactPolicy.BLOCKING:
                raise
            logger.warning(
                "[teardown] agent=%s item=%s outcome=residue reason=%s",
                agent.id,
                artifact.kind,
                type(err).__name__,
            )


@router.delete("/{agent_id}", response_model=Agent)
async def delete_agent(
    agent_id: str,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    pctx: ProjectContext = RBACDepends(get_project_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    """Delete an agent record and, for a registered-external agent, what it owns: the platform
    artifacts first (the one leg that may refuse), then the registered-agent cascade, then the
    record. The project-role gate is MAINTAINER, refused before any teardown.
    """
    existing = await _load_visible_agent(agent_id, ctx)
    _require_agent_project_role(existing, pctx, ProjectRole.MAINTAINER, principal=principal)
    await _teardown_platform_artifacts(existing)
    await _teardown_registered_agent(existing)
    svc = get_service()
    agent = svc.delete(agent_id)
    if not agent:
        raise AgentNotFoundError(f"agent {agent_id} vanished before the delete")
    return agent


@router.post("/{agent_id}/submit", response_model=Agent)
async def submit_agent(
    agent_id: str,
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    await _load_visible_agent(agent_id, ctx)
    svc = get_service()
    agent = svc.submit_for_approval(agent_id)
    if not agent:
        raise AgentNotFoundError(f"agent {agent_id} vanished before the submit")
    return agent


@router.post("/{agent_id}/transitions", response_model=Agent)
async def transition_agent(
    agent_id: str,
    req: TransitionRequest,
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.ADMIN)),
):
    await _load_visible_agent(agent_id, ctx)
    svc = get_service()
    agent = svc.transition(agent_id, req.action, req.reason)
    if not agent:
        raise AgentNotFoundError(f"agent {agent_id} vanished before the transition")
    return agent


@router.put("/{agent_id}/publish", response_model=Agent)
async def publish_agent(
    agent_id: str,
    req: PublishToggle,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    pctx: ProjectContext = RBACDepends(get_project_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    """Flip the cross-tenant publish flag: a platform write that re-asserts an APPROVED record, at the
    MAINTAINER threshold because it widens the agent's exposure.
    """
    agent = await _load_visible_agent(agent_id, ctx)
    _require_agent_project_role(agent, pctx, ProjectRole.MAINTAINER, principal=principal)
    svc = get_service()
    agent = await anyio.to_thread.run_sync(svc.set_published, agent_id, req.published)
    if not agent:
        raise AgentNotFoundError(f"agent {agent_id} vanished before the publish toggle")
    return agent


@router.post("/{agent_id}/reprovision", response_model=Agent, status_code=202)
async def reprovision_agent(
    agent_id: str,
    background_tasks: BackgroundTasks,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    pctx: ProjectContext = RBACDepends(get_project_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    """Re-run identity provisioning for a governed agent: set ``identity_status='pending'``, persist,
    then schedule ``provision`` and the Langfuse hook in the background. The recovery affordance
    for a 'failed' or stranded 'pending' agent; 409 for an agent with no runtime to bind.
    """
    agent = await _load_visible_agent(agent_id, ctx)
    _require_agent_project_role(agent, pctx, ProjectRole.MAINTAINER, principal=principal)
    svc = get_service()
    if not (is_agentcore_agent(agent) or is_databricks_governed_agent(agent)):
        raise AgentRequestError(f"agent {agent_id} has no runtime to bind", kind="not_governed")

    agent.identity_status = IdentityStatus.PENDING
    await anyio.to_thread.run_sync(svc.persist_identity, agent)
    background_tasks.add_task(
        get_identity_service().provision,
        agent,
        runtime_binding=platform_runtime_binding,
    )
    background_tasks.add_task(provision_langfuse_best_effort, agent, get_langfuse_service())
    return agent


class InvokeRequest(WireModel):
    prompt: str = Field(description='Free-form prompt, forwarded to the runtime as {"prompt": ...}.')


class InvokeResult(WireModel):
    """The adapter's answer: ``response`` is the runtime's own payload, text or JSON; ``session_id``
    and ``tool_calls`` are ``None`` on a platform that has neither."""

    response: Any
    session_id: str | None
    tool_calls: list[dict[str, Any]] | None


def _sse_payloads(body: str) -> tuple[object, object]:
    """``(last message-shaped payload, last parseable payload)`` over the ``data:`` lines of an SSE
    body; a payload is message-shaped when it is a dict carrying ``message`` or ``result``."""
    last_message = None
    last_payload = None
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line.startswith("data:"):
            continue
        data = line[len("data:") :].strip()
        if not data or data == "[DONE]":
            continue
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            continue
        last_payload = payload
        if isinstance(payload, dict) and ("message" in payload or "result" in payload):
            last_message = payload
    return last_message, last_payload


def _message_text(target: dict):
    """The joined ``content[].text`` of a Strands ``message``, else the payload's ``result``, else
    the payload itself."""
    message = target.get("message")
    if isinstance(message, dict) and isinstance(message.get("content"), list):
        texts = [
            part.get("text", "") for part in message["content"] if isinstance(part, dict) and part.get("text")
        ]
        if texts:
            return "".join(texts)
    if "result" in target:
        return target["result"]
    return target


def _extract_sse_text(body: str):
    """The terminal response of a buffered ``text/event-stream`` body.

    The AgentCore runtime streams ``data: {...}`` lines; a Strands agent's terminal event is
    ``data: {"message": {"role": "assistant", "content": [{"text": "…"}]}}``. The last
    message-shaped payload wins, then the last parseable payload, then the raw text, so a
    non-Strands streaming agent still yields something. ``tests/test_invoke_route.py`` fences the
    shape with a body captured from the agent template.
    """
    last_message, last_payload = _sse_payloads(body)
    target = last_message if last_message is not None else last_payload
    if isinstance(target, dict):
        return _message_text(target)
    if target is not None:
        return target
    return body


@router.post("/{agent_id}/invoke", response_model=InvokeResult)
async def invoke_agent(
    agent_id: str,
    req: InvokeRequest,
    request: Request,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.VIEWER)),
    stage: Optional[str] = Query(default=None),
):
    """Invoke a governed agent: load and gate the record, OBO-exchange the caller's inbound token for
    an agent-audience token, then dispatch to the platform adapter.

    ``?stage=`` selects that stage's runtime and refuses an unknown one with 404 before any Entra
    round-trip; omitted, the runtime ``agent.agent_arn`` names is invoked. An AgentCore target's
    ARN is shape-checked before the exchange, since a record written before the write-time guard
    can still carry a bad region. The exchange precedes the dispatch by structure, so the Entra
    grant is checked before any platform is contacted on either platform.
    """
    agent = await _load_visible_agent(agent_id, ctx)
    if agent.identity_status != "provisioned" or not (
        is_agentcore_agent(agent) or is_databricks_governed_agent(agent)
    ):
        raise InvokeError(f"agent {agent_id} has no provisioned identity", kind="not_provisioned")

    if stage is not None and not agent.runtime_arns().get(stage):
        raise InvokeError(f"agent {agent_id} owns no runtime on the requested stage", kind="unknown_stage")

    if is_agentcore_agent(agent):
        agent_arn = (agent.agent_arn or "") if stage is None else (agent.runtime_arns().get(stage) or "")
        arn_parts = agent_arn.split(":")
        if len(arn_parts) < 4 or not arn_parts[3] or not AWS_ARN_REGION_RE.fullmatch(arn_parts[3]):
            raise InvokeError("the agent ARN's region segment is not a region", kind="agent_arn_invalid")

    auth_header = request.headers.get("authorization", "")
    user_token = auth_header.split(" ", 1)[1] if " " in auth_header else ""

    who = principal.email or principal.oid or "the caller"
    obo_token = await get_graph_service().obo_exchange(user_token, agent.entra_app_audience or "", caller=who)

    adapter = adapter_for(get_adapters(), _governable_platform(agent))
    return await adapter.invoke(agent, req, principal, obo_token, stage, actor_id=principal.oid)


@router.get("/{agent_id}/runtime", response_model=RuntimeStatus)
async def get_agent_runtime_status(
    agent_id: str,
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.VIEWER)),
    stage: Optional[str] = Query(default=None),
):
    """Live runtime status for one agent, dispatched by platform through ``status_adapter``; the probe
    never raises, so an unreachable control plane reads as ``unknown`` with a hint. ``?stage=``
    probes that stage's runtime; omitted, the runtime ``agent_arn`` names, with the stage attributed
    when the record can. The read's live env drives ``reconcile_runtime_mcp_env``, the one accepted
    write on this GET, and is never serialized.
    """
    agent = await _load_visible_agent(agent_id, ctx)
    status = await anyio.to_thread.run_sync(status_adapter(agent).runtime_status, agent, stage)
    await reconcile_runtime_mcp_env(agent, status.environment_variables)
    return status


@router.get("/{agent_id}/deployments", response_model=List[Deployment])
async def list_agent_deployments(
    agent_id: str,
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.VIEWER)),
    stage: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
):
    """One agent's delivery history, newest first, under ``/runtime``'s gate. The repo is resolved
    server-side from the agent id, never taken from the caller; an agent with no repository has no
    history and answers ``[]``. ``stage`` is free-form and ``None`` means every stage; ``limit``
    mirrors ``/traces``. The store degrades to ``[]`` and never raises.
    """
    from api.routes.projects import get_project_service

    agent = await _load_visible_agent(agent_id, ctx)
    svc = get_project_service()
    repo = await anyio.to_thread.run_sync(svc.find_repository_by_agent_id, agent.id)
    if repo is None:
        return []
    return await anyio.to_thread.run_sync(
        functools.partial(svc.list_deployments, repo.id, stage=stage, limit=limit)
    )


async def _agent_repository(agent_id: str, ctx: TenantContext):
    """The visible agent's repository, resolved server-side (never a caller-supplied repo id),
    or 404 "No repository for this agent" — a detail distinct from the visibility 404."""
    from api.routes.projects import get_project_service

    agent = await _load_visible_agent(agent_id, ctx)
    svc = get_project_service()
    found = await anyio.to_thread.run_sync(svc.find_repository_by_agent_id, agent.id)
    repo = await anyio.to_thread.run_sync(svc.get_repo, found.id) if found is not None else None
    if repo is None:
        raise ProjectError(f"agent {agent.id} has no repository", kind="no_repository")
    return repo


@router.get("/{agent_id}/repository", response_model=AgentRepositoryView)
async def get_agent_repository(
    agent_id: str,
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.VIEWER)),
):
    """The agent → repository edge for readers of the agent, under ``/deployments``' gate: the repo's
    ids and name and its read-time eval gate for the prod candidate.
    """
    repo = await _agent_repository(agent_id, ctx)
    return {
        "project_id": repo.project_id,
        "repo_id": repo.id,
        "name": repo.name,
        "prod_candidate_status": repo.prod_candidate_status,
        "eval_gate": repo.eval_gate,
        "eval_regressions": repo.eval_regressions,
    }


@router.get("/{agent_id}/evals/runs", response_model=EvalRuns)
async def list_agent_eval_runs(
    agent_id: str,
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.VIEWER)),
):
    """The agent's eval runs (summaries, gate, overrides) for readers of the agent — the same
    body as the project route's runs list, under the ``/deployments`` gate: reads are
    tenant-wide here, as for every per-agent read in this module. Item results (inputs and
    outputs) and the per-item detail of gate reasons (tool arguments) stay behind the project
    route; override reasons and oids are readable on purpose (governance review)."""
    import api.routes.evals as evals

    repo = await _agent_repository(agent_id, ctx)
    out = await anyio.to_thread.run_sync(evals.get_eval_runner().runs, repo.project_id, repo.id)
    body = evals.runs_payload(out)
    for run in body["runs"] + ([body["current_run"]] if body["current_run"] else []):
        _redact_run(run)
    return body


def _redact_run(run: dict) -> None:
    """Tenant viewers get verdicts, not data: a per-item check reason ``"<item>: <check> failed —
    <detail>"`` keeps only ``"<item>: <check> failed"``, since the detail quotes expected and actual
    tool arguments. The project-gated routes keep the full detail.
    """
    gate = run.get("gate")
    if gate:
        gate["reasons"] = [r.split(" — ", 1)[0] if " failed — " in r else r for r in gate["reasons"]]


@router.get("/{agent_id}/metrics", response_model=AgentMetrics)
async def get_agent_metrics(
    agent_id: str,
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.VIEWER)),
    date_from: Optional[date] = Query(default=None),
    date_to: Optional[date] = Query(default=None),
):
    """Per-agent Langfuse cost, token and trace metrics over ``[date_from, date_to]``, defaulting to
    the trailing 30 days; the service degrades an unprovisioned read to zeroed metrics. The key
    value never touches this response.
    """
    from api.routes.observability import get_metrics_service

    agent = await _load_visible_agent(agent_id, ctx)
    date_to = date_to or date.today()
    date_from = date_from or (date_to - timedelta(days=30))
    return await get_metrics_service().get_agent_metrics(agent, date_from, date_to)


@router.get("/{agent_id}/traces", response_model=TracePage)
async def get_agent_traces(
    agent_id: str,
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.VIEWER)),
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=50, ge=1, le=100),
):
    """Paged per-agent trace list, ``{"data": [TraceRow], "total": int}``; the service degrades an
    unprovisioned read to an empty page.
    """
    from api.routes.observability import get_metrics_service

    agent = await _load_visible_agent(agent_id, ctx)
    return await get_metrics_service().get_agent_traces(agent, page=page, limit=limit)
