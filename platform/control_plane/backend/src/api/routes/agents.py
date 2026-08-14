"""Agent Registry CRUD + lifecycle API routes (Epic 4, Task 3).

Backed by `AgentRegistryService` (AWS Agent Registry). Mirrors the router +
lazy `get_service()` + error→HTTP mapping idiom of `operating_model.py`, and the
RBAC `Depends as RBACDepends` + `require_role(Role.X)` trailing-param idiom of
`deployments.py`.

RBAC (Decision 6): list/get = VIEWER; create/update/delete/submit = OPERATOR;
transitions (approve/reject/deprecate) = ADMIN.

`created_by` (Decision 7) comes from the validated `current_principal`, never a
hardcoded "user"; when the create payload leaves sponsor_* blank, it defaults to
the creator (the creator sponsors by default).

Per-project roles (E27/T5): ALL FOUR envelope-MUTATION routes (`PUT`, `DELETE`,
`/reprovision`, `/publish`) ALSO gate on the caller's MAINTAINER authority over the
agent's owning project — but ONLY when `agent.project_id` is set, so a
directly-registered (or pre-E27) agent keeps its exact tenant-gated-only behaviour.
`/publish` is gated for the same reason and at the same level: it writes through the same
`svc.update` path and its blast radius is wider (cross-tenant exposure). Checked IN
ADDITION to tenant
visibility and always AFTER it (`_load_visible_agent` first), so a foreign tenant still
gets the byte-identical 404. READS stay tenant-gated — an agent is discoverable
tenant-wide by design — and the registry LIFECYCLE routes (`/submit`, `/transitions`)
keep their platform `require_role` levels only: a lifecycle decision is the AWS Agent
Registry's act, not a project's (design §6 non-overlap).
"""

import functools
import logging
from datetime import date, timedelta
from typing import List, Optional
from urllib.parse import quote, urlparse
from uuid import uuid4

import anyio.to_thread
import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
from fastapi import Depends as RBACDepends
from pydantic import BaseModel

from core.config import settings
from core.rbac import Principal, Role, current_principal, require_role
from models.agent import (
    Agent,
    AgentCreate,
    AgentUpdate,
    IdentityStatus, LifecycleState,
    Platform,
    RuntimeStatus,
    is_databricks_governed_agent,
)
from models.deployment import Deployment
from models.project_role import ProjectRole
from models.repository import RepoDeleteItemResult
from services.agent_identity_service import (
    AgentIdentityService,
    is_agentcore_agent,
)
from services.agent_mcp_env import reconcile_runtime_mcp_env
from services.agent_registry_service import (
    AgentRegistryService,
    IllegalTransitionError,
    NameTakenError,
    apply_creator_sponsor,
)
from services.graph_service import (
    GraphService,
    NotAssignedError,
    OboConfigError,
)
from services.langfuse_provisioning import LangfuseProvisioningService
from services.project_resolver import ProjectContext
from services.tenant_credentials import StageUnresolvedError, TenantCredentialsError
from services.tenant_resolver import TenantContext, visible
from services.tenant_service import (
    BINDING_FEDERATION,
    BINDING_INVOKE_UNAVAILABLE,
    BINDING_SP_SECRET,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/agents", tags=["agents"])

_svc: Optional[AgentRegistryService] = None
_identity_svc: Optional[AgentIdentityService] = None
_databricks_identity_svc = None  # E29/T6 — the Databricks provisioning half
_databricks_workspace_svc = None  # E29/T7 — the Databricks REST client (invoke path)
_langfuse_svc: Optional[LangfuseProvisioningService] = None


def get_service() -> AgentRegistryService:
    """Lazy AgentRegistryService singleton.

    The registry is addressed by NAME. AWS mints the registryId and it cannot be chosen, so
    Terraform could only publish it through a capture file it read during the PLAN walk —
    before the provisioner that writes it had run — which is what forced a from-zero deploy to
    ``terraform apply`` twice. The NAME is a static config value, and name -> id is one
    ``ListRegistries`` call, so the service resolves it itself on first use (see
    ``core.registry_resolver``).

    ``AGENT_REGISTRY_ID`` is still honoured as an explicit OVERRIDE that short-circuits that
    lookup — six operational scripts pass ids directly and must keep working. Empty (the
    default) means "resolve by name". This singleton being lazy is what makes first-request
    resolution natural: nothing resolves at import time.
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
    """Return the shared GraphService singleton.

    ONE source of the instance: ``routes/grants.py``'s ``get_graph_service`` (so the
    whole app shares one ``httpx.AsyncClient``). This is a thin re-export; tests patch
    the singleton on ``grants`` (``grants_module._graph_svc``) and both this delegate
    and the grants routes observe it.
    """
    from api.routes.grants import get_graph_service as _grants_get_graph_service

    return _grants_get_graph_service()


def get_databricks_identity_service():
    """Lazy ``DatabricksIdentityService`` singleton — the Databricks half of provisioning (E29/T6).

    Imported INSIDE the function, not at module scope, for the same reason the graph delegate
    reaches into ``routes/grants``: it pulls in the tenants route (for the ONE ``TenantService``
    singleton, so a tenant's capability flags and secret ARNs are read from the same instance the
    admin routes write), and a module-level import would make that a cycle.

    Returns ``None`` when no tenants table is configured — an AgentCore-only deployment needs no
    Databricks wiring, and a service built against a nonexistent table would fail on first read
    rather than at construction. ``None`` is NOT a silent skip: ``provision()`` raises when a
    Databricks agent arrives with no collaborator, so the deployment gap surfaces as a persisted
    'failed' with a message instead of an agent stranded at 'pending'.
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
    """Lazy ``DatabricksWorkspaceService`` singleton — the invoke path's token minter (E29/T7).

    A SEPARATE accessor from :func:`get_databricks_identity_service`, not a reach into its
    private collaborator, because the two have different preconditions: the identity service
    needs a tenants table (it resolves capability flags and secret ARNs), while the workspace
    client is stateless and holds no credential of its own — C-2's rule that credentials are
    parameters. So this is always available, and the invoke path's failure modes stay about the
    RECORD rather than about deployment wiring.

    Imported inside the function for symmetry with the sibling accessor: nothing here needs the
    module at import time, and a Databricks-free deployment should not pay for the import.
    """
    global _databricks_workspace_svc
    if _databricks_workspace_svc is None:
        from services.databricks_workspace_service import DatabricksWorkspaceService

        _databricks_workspace_svc = DatabricksWorkspaceService()
    return _databricks_workspace_svc


def get_identity_service() -> AgentIdentityService:
    """Lazy AgentIdentityService singleton (provisioning orchestrator).

    Wires the shared GraphService + the registry singleton; region defaults to the
    registry region (research §1 — the runtime + registry are co-located).

    E29/T6: also wires the Databricks half, which is what makes ``provision()``'s platform
    dispatch reachable at runtime rather than only in tests.
    """
    global _identity_svc
    if _identity_svc is None:
        _identity_svc = AgentIdentityService(
            graph=get_graph_service(),
            registry=get_service(),
            tenant_id=settings.ENTRA_TENANT_ID,
            login_base=settings.ENTRA_LOGIN_BASE,
            region=getattr(settings, "AGENT_REGISTRY_REGION", "") or "us-east-1",
            databricks_identity=get_databricks_identity_service(),
        )
    return _identity_svc


def get_langfuse_service() -> LangfuseProvisioningService:
    """Lazy LangfuseProvisioningService singleton (E26 per-agent project + key).

    Wires the E26 config (host + admin secret) + the registry singleton so the
    provisioner can persist the C1 join (``langfuse_project_id`` /
    ``langfuse_key_secret_name``) onto the agent envelope.

    E36/T13 also wires the TenantService, which the provisioner needs to find WHICH ACCOUNT a
    registered agent's runtime lives in (its per-agent secret has to be created there, and its
    runtime reached there). Imported lazily — ``tenants.py`` is the owner of that singleton and a
    top-level import would be a cycle, exactly as ``get_tenant_ctx`` above.
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


def provision_langfuse_best_effort(
    agent: Agent, service: LangfuseProvisioningService
) -> None:
    """Register-time hook: auto-provision the agent's Langfuse project + key (E26/T4).

    BEST-EFFORT: any failure (Langfuse unreachable, session/tRPC failure, SM error) is
    logged and swallowed — it MUST NOT block/raise out of agent registration. On failure
    the C1 join fields simply stay ``None`` (unprovisioned); a later re-run is idempotent
    (it short-circuits once ``langfuse_project_id`` is set). Skipped entirely when Langfuse
    is not configured (``LANGFUSE_HOST`` empty). No secret VALUE is ever logged here.

    E36/T13 — the RUNTIME half. Provisioning alone left a registered agent's runtime unaware of
    the project it had been given: ``LANGFUSE_HOST`` / ``LANGFUSE_SECRET_NAME`` are written
    declaratively by the terraform module for agents the PLATFORM deploys, and a registered
    agent's runtime is not ours to apply. So on success we hand the join to
    ``wire_agent_runtime``, which injects those two variables and grants the runtime's exec role
    read access to that one secret. It is called only AFTER provisioning succeeds (there is no
    secret to point at otherwise) and it never raises — every leg inside it degrades to a logged
    no-op — but it is wrapped anyway, because this function's contract to ``BackgroundTasks`` is
    that NOTHING escapes it.
    """
    if not settings.LANGFUSE_HOST:
        return
    try:
        joined = service.provision_agent_project(agent)
    except Exception:  # noqa: BLE001 — best-effort; never abort registration
        logger.warning(
            "[agents] Langfuse project provisioning failed for agent %s "
            "(non-blocking; fields stay unprovisioned)",
            agent.id,
        )
        return

    secret_name = (joined or {}).get("secret_name")
    if not secret_name:
        return
    try:
        service.wire_agent_runtime(agent, secret_name, get_identity_service())
    except Exception:  # noqa: BLE001 — best-effort; never abort registration
        logger.warning(
            "[agents] Langfuse runtime wiring failed for agent %s (non-blocking; the project "
            "exists but the runtime was not told about it)",
            agent.id,
        )


async def get_tenant_ctx(
    principal: Principal = RBACDepends(current_principal),
) -> TenantContext:
    """Delegate to the ONE resolver-singleton accessor (E24/T5).

    ``users.py`` owns the lazy ``TenantResolver`` singleton (``_tenant_resolver`` /
    ``get_tenant_resolver()``); this is a thin re-export so agents.py has its own
    per-request ``tenant_ctx`` dependency WITHOUT keeping a second resolver copy —
    tests patch ``api.routes.users._tenant_resolver`` and both /users/me and every
    route here observe it. Imported lazily to avoid an import cycle at module load.
    """
    from api.routes.users import get_tenant_ctx as _users_get_tenant_ctx

    return await _users_get_tenant_ctx(principal)


async def get_project_ctx(
    principal: Principal = RBACDepends(current_principal),
) -> ProjectContext:
    """Delegate to the ONE project-resolver-singleton accessor (E27/T5).

    ``users.py`` owns the lazy ``ProjectResolver`` singleton (``_project_resolver`` /
    ``get_project_resolver()``); this is a thin re-export so agents.py has its own
    per-request ``project_ctx`` dependency WITHOUT keeping a second resolver copy —
    tests patch ``api.routes.users._project_resolver`` and every gated route here
    observes it. Imported lazily to avoid an import cycle at module load (mirrors
    ``get_tenant_ctx`` above, and the identical re-export in ``projects.py``).
    """
    from api.routes.users import get_project_ctx as _users_get_project_ctx

    return await _users_get_project_ctx(principal)


def _require_agent_project_role(
    agent: Agent, pctx: ProjectContext, required: ProjectRole, *, principal: Principal
) -> None:
    """Gate a MUTATION on the caller's authority over the agent's OWNING PROJECT (E27/T5).

    Closes the mutation bypass: without this, any tenant member could update / reprovision /
    delete an agent materialized under someone else's project, routing around the per-project
    role T4 enforces on the project routes themselves.

    CONDITIONAL on ``agent.project_id``. ``None`` means the agent is not project-governed (a
    directly-registered agent, or any pre-E27 envelope) — behaviour is then UNCHANGED,
    tenant-gated only. There is deliberately no fallback that would gate an unparented agent
    on some default project.

    Runs AFTER ``_load_visible_agent``, so a foreign tenant has already 404'd and a 403 here
    can only ever confirm an agent the caller's own tenant already exposes.

    Reuses T4's ``_require_project_role_or_ungoverned`` verbatim rather than reimplementing it,
    so the design-§3 ungoverned-project fallback AND the fail-CLOSED direction on an unreadable
    role partition are IDENTICAL on both surfaces (two differently-behaving gates on the same
    project would be a bypass of whichever is looser) — including the stale-cache refresh the
    gate takes before it serves a refusal, which is why ``principal`` is threaded through here.
    Imported lazily: ``projects.py`` imports THIS module at load time, so a top-level import
    would be a cycle.
    """
    if agent.project_id is None:
        return
    from api.routes.projects import _require_project_role_or_ungoverned

    _require_project_role_or_ungoverned(
        pctx, agent.project_id, required, principal=principal
    )


async def _load_visible_agent(agent_id: str, ctx: TenantContext) -> Agent:
    """Load an agent by id and gate it on tenant visibility BEFORE any side effect.

    ONE helper every detail/mutation/lifecycle/invoke/identity route calls (research
    brief — missing an endpoint is this task's failure mode). A missing OR
    not-visible agent raises the SAME 404 literal ("Agent not found") — the two
    cases must be byte-identical (spec's 404-not-403 contract: a foreign tenant's
    agent must look absent, never leak a 403 that would confirm it exists).
    """
    agent = get_service().get(agent_id)
    if not agent or not visible(ctx, agent.tenant_id):
        raise HTTPException(status_code=404, detail="Agent not found")
    return agent


class TransitionRequest(BaseModel):
    """Body for the admin lifecycle transition endpoint."""

    action: str  # "approve" | "reject" | "deprecate"
    reason: str


class PublishRequest(BaseModel):
    """Body for the cross-tenant publish toggle (E24/T5)."""

    published: bool


def _coerce_lifecycle(value: Optional[str]) -> Optional[LifecycleState]:
    if value is None:
        return None
    try:
        return LifecycleState(value)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid lifecycle_state: {value}")


def _coerce_platform(value: Optional[str]) -> Optional[Platform]:
    if value is None:
        return None
    try:
        return Platform(value)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid platform: {value}")


# --- CRUD --------------------------------------------------------------------

@router.post("", response_model=Agent, status_code=201)
async def create_agent(
    req: AgentCreate,
    background_tasks: BackgroundTasks,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    # Multi-tenancy (E24/T5) — tenant_id must exist, then (for a non-global caller)
    # must be one of the caller's own memberships. No resource exists yet, so 403
    # (not 404) is correct here — unlike the visibility-gated routes below.
    from api.routes.tenants import get_tenant_service
    from services.tenant_service import TenantError

    try:
        get_tenant_service().get(req.tenant_id)
    except TenantError:
        raise HTTPException(status_code=400, detail="unknown tenant")
    if not ctx.is_global and req.tenant_id not in ctx.tenant_ids:
        raise HTTPException(status_code=403, detail="tenant not permitted")

    # The creator sponsors by default when the wizard leaves sponsor_* blank. Shared
    # with the repo-creation path (project_service.add_repo) via this pure helper so
    # both registration flows default the owner identically (research §4).
    apply_creator_sponsor(req, principal)

    svc = get_service()
    try:
        agent = svc.create(req, created_by=principal.email or principal.oid)
    except NameTakenError as e:
        raise HTTPException(status_code=409, detail=str(e))

    # Provisioning-on-registration hook (Epic 6, Decision 5) — governed runtimes only.
    # A manually-created AgentCore Runtime agent (arn + entra + aws_bedrock) gets its
    # Entra identity provisioned in the BACKGROUND. create() already stamped
    # identity_status='pending' INTO the create envelope (so the returned agent — and this
    # 201 response — already shows 'pending'); we no longer do an update-right-after-create
    # here (that 500'd with a ConflictException while the record was still CREATING). We
    # only schedule provision() to run AFTER this 201 is sent — and create() has polled the
    # record to DRAFT, so provision()'s persist_identity writes hit a modifiable record.
    # Errors inside provision() are swallowed into identity_status='failed' by the service
    # itself — they never crash the request. A metadata agent (neither gate) → no
    # provisioning; status stays 'none'.
    #
    # E29/T5, contract C-4: the hook now DISPATCHES over two platforms. The two gates are
    # mutually exclusive by construction (each pins a different `platform`), so if/elif is a
    # dispatch, not a precedence hack — but it is written as if/elif rather than two `if`s so
    # that a future third platform cannot accidentally schedule provision twice.
    #
    # WHY the Databricks arm schedules the SAME `provision(agent)` call today, while
    # `agent_identity_service` still knows only AgentCore: `provision()` starts with
    # `provision_identity`, which is PLATFORM-NEUTRAL — a per-agent Entra app + SP + the two
    # appRoles, all of which a Databricks-hosted agent needs identically. T6 adds the
    # runtime half (federation audience / per-agent SP) behind that same entry point, so this
    # call shape does not change when it lands. Scheduling it now is correct, not premature:
    # a Databricks agent that reached this line with `auth_type == ENTRA` has asked for an
    # Entra identity, and withholding it would leave `identity_status` stuck at 'pending'.
    if is_agentcore_agent(agent):
        background_tasks.add_task(get_identity_service().provision, agent)
    elif is_databricks_governed_agent(agent):
        background_tasks.add_task(get_identity_service().provision, agent)

    # Langfuse observability hook (Epic 26/T4) — auto-provision a per-agent Langfuse
    # project + key at registration so the agent's traces land in its own project
    # (structural attribution — no trace tags). BEST-EFFORT + scheduled AFTER the 201:
    # provision_langfuse_best_effort swallows every failure into a logged no-op (fields
    # stay unprovisioned), so it never blocks/aborts registration; it is idempotent on a
    # later re-run, and a no-op when LANGFUSE_HOST is unset.
    background_tasks.add_task(
        provision_langfuse_best_effort, agent, get_langfuse_service()
    )

    return agent


@router.get("", response_model=List[Agent])
async def list_agents(
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.VIEWER)),
    lifecycle_state: Optional[str] = Query(default=None),
    sponsor_oid: Optional[str] = Query(default=None),
    business_unit: Optional[str] = Query(default=None),
    region: Optional[str] = Query(default=None),
    platform: Optional[str] = Query(default=None),
):
    svc = get_service()
    records = svc.list(
        lifecycle_state=_coerce_lifecycle(lifecycle_state),
        sponsor_oid=sponsor_oid,
        business_unit=business_unit,
        region=region,
        platform=_coerce_platform(platform),
    )
    # Tenant post-filter (E24/T5) — the AWS registry API has no tenant concept, so
    # filtering lands AFTER svc.list() returns (research §5).
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
    # Project-role gate (E27/T5): editing a project's agent is a MAINTAINER act — the same
    # threshold materializing it carries. Refused BEFORE the registry write.
    agent = await _load_visible_agent(agent_id, ctx)
    _require_agent_project_role(agent, pctx, ProjectRole.MAINTAINER, principal=principal)
    svc = get_service()
    try:
        agent = svc.update(agent_id, req)
    except NameTakenError as e:
        raise HTTPException(status_code=409, detail=str(e))
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    return agent


async def _teardown_item(item: str, start) -> RepoDeleteItemResult:
    """Run one best-effort teardown leg and map success/failure to a report line-item.

    The async twin of ``ProjectService._run_step`` (E23/T4), in the same vocabulary and the
    same model: ``outcome`` ∈ ``deleted|failed|skipped`` and a SAFE ``reason`` — T8's two
    PREFIXED reasons (``assume_role_failed:`` = we know the owning account and could not get
    in, ``stage_unresolved:`` = we cannot tell which account owns it; two different operator
    actions, which is why they are not one) and ``type(err).__name__`` for everything else,
    never a token, a Graph body or an AWS message. The ``langfuse`` leg propagates BOTH
    prefixed classes by design, so without these arms the identical cross-account failure
    would read differently here and on the repo path. The identical helper lives in
    ``routes/mcp_servers.py``; the two registry route modules are deliberate structural clones
    (see both module docstrings) and do not import each other's privates.

    ``start`` is a zero-arg CALLABLE returning the awaitable, not the awaitable itself, so
    that resolving the lazy service singleton happens INSIDE the guard too: a cascade whose
    contract is "nothing escapes" must not be able to 500 on building a collaborator.

    A leg may RETURN a non-empty ``str`` to mean "I deliberately did nothing, and this is the
    safe reason": that becomes ``skipped`` with that reason instead of a false ``deleted``.
    """
    try:
        note = await start()
    except TenantCredentialsError as err:
        logger.exception("[teardown] step '%s' could not assume the tenant role", item)
        return RepoDeleteItemResult(
            item=item, outcome="failed", reason=f"assume_role_failed: {err.message}"
        )
    except StageUnresolvedError as err:
        logger.exception("[teardown] step '%s' could not resolve the owning account", item)
        return RepoDeleteItemResult(
            item=item, outcome="failed", reason=f"stage_unresolved: {err.message}"
        )
    except Exception as err:  # noqa: BLE001 — best-effort; the report carries the failure
        logger.exception("[teardown] step '%s' failed", item)
        return RepoDeleteItemResult(item=item, outcome="failed", reason=type(err).__name__)
    if isinstance(note, str) and note:
        return RepoDeleteItemResult(item=item, outcome="skipped", reason=note)
    return RepoDeleteItemResult(item=item, outcome="deleted")


async def _owned_by_a_repository(agent_id: str) -> bool:
    """Does a Repository own ``agent_id``? The authoritative "this is not a registered-external
    agent" answer (E36/T16 fix round 1), asked of the repository partition rather than inferred
    from ``project_id``, which is also blank on every pre-E27 envelope.

    ``ProjectService.find_repository_by_agent_id`` is the same call the builds route uses to
    bind an OIDC repo identity to the agent it claims to build. Imported LAZILY because the
    module-level edge runs the other way — ``routes/projects`` imports ``get_identity_service``
    / ``get_langfuse_service`` / ``get_service`` FROM this module at import time
    (``projects.py:61``) — so a module-level import of it here would close an import cycle. The
    same lazy import is already the idiom in this module (see ``/deployments`` below).

    FAILS CLOSED, i.e. returns True on any error: an unanswerable ownership question must not
    authorize an irreversible teardown of a possibly repo-owned agent's identity, and it must
    not turn the delete into a 500 either. The record still goes; a genuinely registered agent's
    orphans are reclaimed by re-running the delete once the store answers again."""
    from api.routes.projects import get_project_service

    try:
        repo = await anyio.to_thread.run_sync(
            get_project_service().find_repository_by_agent_id, agent_id
        )
    except Exception:  # noqa: BLE001 — unresolved ownership must not authorize a teardown
        logger.exception(
            "[teardown] could not establish whether a repository owns agent %s — skipping "
            "every teardown leg",
            agent_id,
        )
        return True
    return repo is not None


async def _teardown_registered_agent(agent: Agent) -> List[RepoDeleteItemResult]:
    """Tear down what a REGISTERED-EXTERNAL agent owns, before its record goes (E36/T16).

    Every teardown in the platform used to hang off the repo-delete cascade
    (``DELETE /projects/{id}/repos/{repo_id}``), which an agent registered through
    ``POST /agents`` never has. So deleting its record orphaned, permanently: its Entra
    app + service principal (and the backend→agent consent granted on them), its
    ``agp-agent-obo-{id}`` credential provider in the AgentCore Token Vault, and its Langfuse
    project + the Secrets Manager secret holding that project's keys.

    A REPOSITORY-OWNED agent is a no-op here — every item ``skipped`` — because the repo
    cascade owns its teardown and does more than this can (runtime, image, exec role, TF state)
    with an operator-selected item set. Running both would double-delete on one path and split
    the report across two. That check lives HERE rather than at the call site so there is ONE
    decision point and both directions are directly testable.

    OWNERSHIP IS ASKED OF THE REPOSITORY PARTITION, not inferred from ``project_id``
    (E36/T16 fix round 1). ``project_id is None`` also matches a PRE-E27 envelope — the very
    case ``_require_agent_project_role``'s docstring calls out — and for one of those this
    cascade would have deleted a live agent's Entra app, vault entry and Langfuse project from
    a MAINTAINER-gated route while its repo, runtime and image survived, bypassing the OWNER
    gate those resources sit behind. ``ProjectService.find_repository_by_agent_id`` is the
    authoritative answer and already exists; ``project_id`` stays as a cheap fast path (set
    only by ``add_repo``, so it is sufficient on its own) that avoids the repo scan for the
    common materialized delete. A lookup that RAISES skips both legs: an irreversible teardown
    must not be authorized by an unanswered ownership question, and it must not 500 a delete
    either.

    Three legs, each BEST-EFFORT and each its own report line-item:

      1. ``obo_provider`` — the ``agp-agent-obo-{id}`` Token Vault entry
         (``AgentIdentityService.delete_obo_provider``). Its OWN item rather than riding
         ``identity``: nothing is blocking on this path, so a surviving vault entry must be
         reported, not swallowed into an ``identity: deleted`` that is then false. FIRST
         because the Entra app is the thing whose disappearance makes the entry dangling.
      2. ``identity`` — ``delete_identity(..., include_obo_provider=False)``: the Entra app,
         whose deletion cascades the SP and its consents. The SAME method the repo cascade
         uses (which keeps the bundled provider leg, because ITS ``identity`` item is
         blocking), so the two paths cannot drift; the opt-out is what stops a second,
         redundant delete call for the resource leg 1 already reported.
      3. ``langfuse`` — ``LangfuseProvisioningService.delete_agent_project``: the Langfuse
         project + the per-agent secret, the latter deleted in the account that HOLDS it
         (E36/T16's account-resolved teardown). Off-loaded via ``anyio.to_thread.run_sync``:
         it is sync ``requests`` + boto3 and must not block the uvicorn loop.

    NONE can block the record delete. The record is the thing the operator asked to
    remove, it is the only durable pointer to these resources, and every leg is idempotent —
    so a failure is REPORTED (logged with a stack trace, one line per resource) and the
    record still goes. The honest cost is named in ``docs/agentcore-registration.md``: once
    the record is gone a failed leg must be reclaimed by hand, which is why the report is
    per-resource rather than a single boolean.
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
        # No provider was ever created (the agent was never granted an MCP).
        items.append(RepoDeleteItemResult(item="obo_provider", outcome="skipped"))

    if agent.entra_app_id or agent.entra_sp_id:
        items.append(
            await _teardown_item(
                "identity",
                lambda: get_identity_service().delete_identity(
                    agent, include_obo_provider=False
                ),
            )
        )
    else:
        # Nothing was ever minted for this agent (a metadata-only registration): no Entra
        # object. An explicit skipped item, never a silent absence.
        items.append(RepoDeleteItemResult(item="identity", outcome="skipped"))

    if settings.LANGFUSE_HOST:
        items.append(
            await _teardown_item(
                "langfuse",
                lambda: anyio.to_thread.run_sync(
                    get_langfuse_service().delete_agent_project, agent
                ),
            )
        )
    else:
        # Langfuse unconfigured — the register-time hook never provisioned anything either
        # (``provision_langfuse_best_effort`` short-circuits on the same check).
        items.append(RepoDeleteItemResult(item="langfuse", outcome="skipped"))

    # The report's ONLY consumer: `response_model=Agent` is the pinned wire contract, so this
    # log line IS the audit trail for an operator who has to reclaim a failed leg by hand once
    # the record — the only pointer to these resources — is gone. ONE stable prefix across both
    # REGISTRY cascades so a single CloudWatch filter finds every outcome they report (the repo
    # cascade logs under `[project] teardown step …` and returns its items on the wire).
    for entry in items:
        logger.info(
            "[teardown] agent=%s item=%s outcome=%s reason=%s",
            agent.id,
            entry.item,
            entry.outcome,
            entry.reason,
        )
    return items


def _has_databricks_residue(agent: Agent) -> bool:
    """Does this record name anything that outlives it on the Databricks side?

    ``binding_mode`` covers the federation audience (``_remove_audience`` is itself gated on
    ``binding_mode == "federation"``); the two SP fields cover sp_secret mode INCLUDING the
    partial state where the principal exists but its secret was never stored.

    Nothing here ⇒ nothing to tear down, which is what keeps a registered-but-never-provisioned
    Databricks agent deletable on a deployment that has no Databricks wiring at all.
    """
    return bool(agent.binding_mode or agent.databricks_sp_id or agent.databricks_sp_secret_arn)


async def _teardown_databricks_runtime(agent: Agent) -> None:
    """Remove the Databricks-side residue of ``agent`` before its record is deleted.

    WHY THIS LIVES ON THE ROUTE. The E23 runtime cascade
    (``DELETE /projects/{id}/repos/{repo_id}``) cannot reach a Databricks agent: repo
    materialization pins ``platform=Platform.AWS_BEDROCK`` (``project_service``), so every agent
    that cascade ever sees is an AgentCore one. ``DELETE /agents/{id}`` is the path a Databricks
    agent actually travels, so the teardown dispatches here — by platform, mirroring how
    ``provision`` and ``/reprovision`` dispatch their runtime halves.

    GATED ON ``platform``, NOT ON ``is_databricks_governed_agent``. The governed gate also
    requires ``auth_type == ENTRA`` and a ``runtime_handle``, either of which a later edit can
    drop from a record that was ALREADY provisioned — and a record with live residue and a
    broken gate is exactly the one that must still be cleaned up. This predicate is strictly
    wider than the governed gate and cannot match an AgentCore record, whose delete path is
    therefore unchanged.

    RAISES on a failure the caller must not delete through: see
    ``DatabricksIdentityService.delete_databricks_runtime`` for which items block (the
    federation audience — the customer's live trust state) and which only report themselves
    (our own secret, and the service principal the workspace client cannot delete).
    """
    if agent.platform != Platform.DATABRICKS or not _has_databricks_residue(agent):
        return

    databricks = get_databricks_identity_service()
    if databricks is None:
        # A record carrying live Databricks state on a deployment with no Databricks wiring.
        # REFUSED rather than deleted: dropping the record here would delete the only thing
        # that still knows WHICH audience to withdraw from the customer's account. 500, not
        # 502 — nothing upstream was asked; this is a gap in this deployment's configuration.
        logger.error(
            "[agents] refusing to delete Databricks agent %s: it carries Databricks state but "
            "no Databricks identity service is wired, so its account-level trust entry cannot "
            "be removed",
            agent.id,
        )
        raise HTTPException(
            status_code=500,
            detail="the agent's Databricks bindings cannot be removed on this deployment, "
                   "so it was not deleted",
        )

    try:
        await databricks.delete_databricks_runtime(agent)
    except HTTPException:
        raise
    except Exception as err:  # noqa: BLE001 — ProvisioningError or a transport fault
        logger.warning(
            "[agents] Databricks teardown failed for agent %s: %s",
            agent.id,
            type(err).__name__,
        )
        # The record SURVIVES, which is what makes the retry possible: it is the only place the
        # audience to withdraw is still written down.
        raise HTTPException(
            status_code=502,
            detail="the agent's Databricks federation audience could not be removed, so the "
                   "agent was not deleted — retry, or ask your Databricks account admin",
        ) from err


@router.delete("/{agent_id}", response_model=Agent)
async def delete_agent(
    agent_id: str,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    pctx: ProjectContext = RBACDepends(get_project_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    """Delete an agent record — and, for a REGISTERED-EXTERNAL agent, what it owns.

    E36/T16: this was record-only for every agent, which for a registered agent (no repo,
    hence no repo cascade) orphaned its Entra app/SP, its OBO credential provider and its
    Langfuse project + secret forever. ``_teardown_registered_agent`` now runs FIRST — the
    ids it needs live on the record — and is a no-op for an agent a REPOSITORY owns, whose
    teardown belongs to the OWNER-gated per-item ``DELETE /projects/{id}/repos/{repo_id}``.

    E29: a DATABRICKS agent's runtime residue (federation audience, service principal,
    secret) is torn down before that cascade by ``_teardown_databricks_runtime`` — the one
    leg that may REFUSE the delete, because the audience is the customer's live trust state
    and this record is its only pointer.

    The project-role gate (E27/T5) stays at MAINTAINER, unchanged: the cascade added here is
    the teardown of resources this record is the only pointer to, not the wider
    runtime/image/exec-role reclaim that sits behind OWNER on the repo route. Refused BEFORE
    any teardown or registry write.
    """
    existing = await _load_visible_agent(agent_id, ctx)
    _require_agent_project_role(existing, pctx, ProjectRole.MAINTAINER, principal=principal)
    # E29 (final review, Critical): tear the Databricks side down BEFORE anything else. The
    # order is load-bearing twice over — the record is the only thing that names the audience,
    # the service principal and the secret (so it must outlive them), and this is the ONE leg
    # that may REFUSE the delete (the federation audience is the customer's live trust state),
    # so it runs before any best-effort leg makes progress. A no-op for every non-Databricks
    # agent.
    await _teardown_databricks_runtime(existing)
    # E36/T16: the registered-agent cascade — OBO provider, Entra identity, Langfuse — each
    # leg best-effort, reported, never blocking the record delete. The per-agent Entra app
    # (E29 livefix-8's orphan, found live in B6.1) is this cascade's ``identity`` line-item
    # for Databricks agents too: they are registered-external and never repository-owned, so
    # the cascade runs for them; a second, blocking delete call here would double-delete the
    # same resource under contradictory failure semantics.
    await _teardown_registered_agent(existing)
    svc = get_service()
    agent = svc.delete(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    return agent


# --- lifecycle ---------------------------------------------------------------

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
        raise HTTPException(status_code=404, detail="Agent not found")
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
    try:
        agent = svc.transition(agent_id, req.action, req.reason)
    except IllegalTransitionError as e:
        # Illegal status edge (e.g. DRAFT->APPROVED). IllegalTransitionError IS a
        # ValueError, so this MUST precede the generic ValueError->400 handler.
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    return agent


@router.put("/{agent_id}/publish", response_model=Agent)
async def publish_agent(
    agent_id: str,
    req: PublishRequest,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    pctx: ProjectContext = RBACDepends(get_project_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    """Flip the cross-tenant publish flag (E24/T5). Visibility-gated 404 (same as
    every other mutation), then a plain envelope update via the existing
    ``AgentUpdate`` read-modify-write path."""
    # Project-role gate (E27/T5): publish writes through the SAME ``svc.update``
    # read-modify-write as ``PUT /agents/{id}``, and its blast radius is WIDER — it flips
    # the cross-tenant exposure switch. So it sits at the same MAINTAINER threshold; a
    # caller refused on a `purpose` edit must not be able to publish that agent instead.
    agent = await _load_visible_agent(agent_id, ctx)
    _require_agent_project_role(agent, pctx, ProjectRole.MAINTAINER, principal=principal)
    svc = get_service()
    agent = svc.update(agent_id, AgentUpdate(published=req.published))
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    return agent


# --- identity re-provisioning (Epic 6, CRITIQUE-FIX-G) -----------------------

@router.post("/{agent_id}/reprovision", response_model=Agent, status_code=202)
async def reprovision_agent(
    agent_id: str,
    background_tasks: BackgroundTasks,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    pctx: ProjectContext = RBACDepends(get_project_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    """Re-run identity provisioning for an AgentCore agent (any state).

    The provisioning sequence is non-atomic + idempotent, so this is the recovery
    affordance for a 'failed' (or stranded 'pending') agent — and also re-configures a
    live 'provisioned' one. We FIRST set identity_status='pending' and persist
    (CRITIQUE-FIX-G — so the invoke-409 gate + the FE banner reflect the in-flight
    re-config: UpdateAgentRuntime puts the runtime into UPDATING, and provision() only
    flips back to 'provisioned' after poll-to-READY), THEN schedule provision() as a
    background task. Returns 202 with the pending agent.

    E29/T6: "AgentCore agent" above is now "governed agent" — `provision()` dispatches its
    runtime half by platform, so this route accepts an AgentCore OR a Databricks-governed
    agent and 409s only an agent that has no runtime to bind at all.
    """
    # Project-role gate (E27/T5): re-provisioning rewrites the agent's Entra identity and
    # re-configures its live runtime — a MAINTAINER act, refused BEFORE the pending flip
    # (which itself is a persisted state change that blocks /invoke).
    agent = await _load_visible_agent(agent_id, ctx)
    _require_agent_project_role(agent, pctx, ProjectRole.MAINTAINER, principal=principal)
    svc = get_service()
    # E29/T6 (ledger OB-1): dispatch by PLATFORM, mirroring the create hook's if/elif.
    # Before this, the gate was `not is_agentcore_agent` — so a Databricks-governed agent
    # stranded at 'failed' or 'pending' had NO recovery affordance, and the resumable
    # provisioning the whole design rests on was unreachable through the API. The 409 still
    # stands for a metadata-only agent, which genuinely has nothing to provision.
    if not (is_agentcore_agent(agent) or is_databricks_governed_agent(agent)):
        raise HTTPException(status_code=409, detail="not a governed agent")

    agent.identity_status = IdentityStatus.PENDING
    svc.persist_identity(agent)
    background_tasks.add_task(get_identity_service().provision, agent)
    # Reprovision is the RECOVERY affordance, so it must resume EVERY registration leg —
    # including this one. Without it, an agent whose first registration failed at identity
    # never gets a Langfuse project: the register-time hook is queued BEHIND provision(),
    # and starlette's BackgroundTasks stop at the first raising task, so the identity
    # failure starves it — and this route used to resume only the identity half. Every leg
    # of the hook is idempotent (provision_agent_project short-circuits on an existing
    # langfuse_project_id; the runtime env write is a merge), so a converged agent no-ops.
    # Same order as the register route: the hook's runtime env-merge must not race
    # provision()'s authorizer full-replace.
    background_tasks.add_task(
        provision_langfuse_best_effort, agent, get_langfuse_service()
    )
    return agent


# --- invoke (Epic 6, research §3 — httpx bearer-POST + SSE) -------------------

class InvokeRequest(BaseModel):
    prompt: str  # freeform; forwarded to the runtime as {"prompt": ...}


def _extract_sse_text(body: str):
    """Buffer an SSE (``text/event-stream``) body and extract the terminal response.

    The AgentCore runtime STREAMS ``data: {...}`` lines (research §3, live-confirmed).
    A Strands agent's terminal event is
    ``data: {"message": {"role": "assistant", "content": [{"text": "…"}]}}`` — we walk
    the ``data:`` lines, keep the LAST one carrying a ``message``/``result``, and pull
    the joined ``content[].text``. Falls back to the last parseable data payload (or
    the raw buffered text) if no message-shaped event is found, so a non-Strands
    streaming agent still yields something useful.

    The contract's HOME is the agent template's README ("Response contract" in
    ``agent-templates/strands-agentcore/README.md``) — that is what a customer writing
    an agent reads. This docstring describes the consumer, not the agreement; a body
    captured from that template fences the two together in
    ``tests/test_invoke_route.py``.
    """
    import json

    last_message = None
    last_payload = None
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line.startswith("data:"):
            continue
        data = line[len("data:"):].strip()
        if not data or data == "[DONE]":
            continue
        try:
            payload = json.loads(data)
        except (ValueError, TypeError):
            continue
        last_payload = payload
        if isinstance(payload, dict) and ("message" in payload or "result" in payload):
            last_message = payload

    target = last_message if last_message is not None else last_payload
    if isinstance(target, dict):
        message = target.get("message")
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, list):
                texts = [
                    part.get("text", "")
                    for part in content
                    if isinstance(part, dict) and part.get("text")
                ]
                if texts:
                    return "".join(texts)
        if "result" in target:
            return target["result"]
        return target
    if target is not None:
        return target
    # No data: lines parsed — return the raw text so the caller still sees something.
    return body


# --- the Databricks invoke leg (E29/T7, contract C-5, design §3) -------------

# The ONE host suffix a Databricks App is served from. Validated with a LEADING DOT so the
# check is a real subdomain test: `.databricksapps.com` refuses both the bare apex
# (`databricksapps.com`) and the suffix lookalike (`evil-databricksapps.com`), which a plain
# `endswith("databricksapps.com")` would have accepted — and accepting either means attaching
# a live Databricks bearer token to a request aimed at a host the customer does not own.
#
# Serving endpoints are served from the WORKSPACE host, not this one, and are deliberately NOT
# accepted here: this epic governs Apps only (design §3), and widening the rule for a shape no
# code path can yet produce would be an unexercised allowance on the one route that carries a
# token outbound. When serving endpoints land, the rule grows a second arm — host EQUALS the
# resolved stage's workspace host — which is a stricter test than this one, not a looser one.
_DATABRICKS_APPS_HOST_SUFFIX = ".databricksapps.com"

# The agent template's default route. Used only when the record carries no `endpoint_url`.
_DATABRICKS_DEFAULT_ROUTE = "/api/v1/agent"

# The binding-mode vocabulary — IMPORTED from its single declaration beside the only writer of
# the field (``tenant_service._probe``), not restated (E29/T14a). Three copies of the literals
# was how "federation | sp_secret" could mean two different things in two modules.
_MODE_FEDERATION = BINDING_FEDERATION
_MODE_SP_SECRET = BINDING_SP_SECRET
_MODE_INVOKE_UNAVAILABLE = BINDING_INVOKE_UNAVAILABLE

# The per-agent SP secret body's credential key — the SAME literal
# `databricks_identity_service._AGENT_SECRET_KEY` writes. Restated rather than imported
# because importing a private name would make this route depend on that module's internals;
# the read itself goes through the service's own helpers (see `_databricks_agent_secret`).
_DATABRICKS_AGENT_SECRET_KEY = "client_secret"


# Codes that describe something an OPERATOR can fix, each with a composed sentence naming the
# cause and the record that must change (the `databricks_identity_service._ACTIONABLE_KINDS`
# idiom — fix round 1). Every sentence is written HERE from fixed text: no upstream message, no
# workspace URL, no host. A bare code is greppable but not actionable, and
# `workspace_stage_unresolved` in particular describes a STRUCTURAL limit that an operator
# cannot deduce from the code alone.
_DATABRICKS_ACTIONABLE = {
    "federation_unavailable": (
        "this agent's Databricks tenant cannot federate, so AGP will not invoke it: federation "
        "needs BOTH an account-admin credential on the tenant and this Entra tenant's "
        "identities synced into the Databricks account (user sync). Add them and re-connect the "
        "tenant so its capabilities are re-probed. AGP does not fall back to service-principal "
        "binding, because that would silently change what the audit log can prove"
    ),
    "sp_secret_disabled": (
        "this agent carries the dormant per-agent service-principal Databricks binding, which "
        "this deployment does not allow: every call would be attributed to a service principal "
        "instead of the caller. Set DATABRICKS_ALLOW_SP_SECRET_BINDING=true to enable it "
        "deliberately, or move the tenant to federation (account-admin credential + user sync)"
    ),
    "workspace_stage_unresolved": (
        "AGP cannot tell which of this tenant's Databricks workspaces hosts the agent. A "
        "Databricks Apps hostname carries no workspace identity, so a tenant with more than "
        "one workspace stage cannot be disambiguated from the app URL alone — the fix is on "
        "the tenant record: have provisioning record which workspace stage the agent was "
        "bound in"
    ),
}


def _databricks_502(code: str) -> HTTPException:
    """A 502 carrying a SAFE code, plus an actionable sentence where one exists.

    Every Databricks-side failure on this path answers with one of a fixed set of codes. The
    upstream `DatabricksError.message`/`.kind` is NEVER forwarded, even though C-2 composes it
    safely: an invoke response is the most-read surface in the product, and a workspace path,
    a principal id, or an echoed OAuth form has no business in one. The code is what support
    greps for; the logs (also code-only) are where the correlation happens.

    Fix round 1: a code alone is not always enough. Where the failure is something an operator
    can act on, :data:`_DATABRICKS_ACTIONABLE` supplies a sentence composed from FIXED text —
    never from upstream data — so the redaction rule is unchanged and the message still names
    nothing customer-specific.
    """
    hint = _DATABRICKS_ACTIONABLE.get(code)
    if hint:
        return HTTPException(status_code=502, detail=f"{hint} [{code}]")
    return HTTPException(
        status_code=502, detail=f"the agent could not be invoked [{code}]"
    )


def _validate_databricks_app_url(raw: str) -> str:
    """OB-4. Return ``raw`` if it is an https Databricks-Apps URL; else raise a safe 502.

    THIS IS THE GUARD ON A CREDENTIAL, not a formatting nicety. Both `runtime_handle` and
    `endpoint_url` are fields on `AgentBase`, so both arrive CLIENT-SETTABLE from registration
    — and this is the one place in the platform that attaches a freshly-obtained Databricks
    bearer token to an outbound request. An unvalidated handle is a registration field that
    exfiltrates a token to a host of the registrant's choosing.

    Four things are checked, and each one is a real attack that the others do not catch:

    * **scheme is exactly https** — `http://` would put the token on the wire in clear, and
      `javascript:`/scheme-relative (`//host`) forms parse to no scheme at all.
    * **no userinfo** — `https://app.aws.databricksapps.com@attacker.example` has a *hostname*
      of `attacker.example`; the trusted-looking part is a username. This is the classic
      allowlist bypass, and it is refused on the SHAPE (any `@` in the netloc) rather than by
      trusting the parse, because the parse is exactly what the trick targets.
    * **host ends with a DOTTED suffix** — see :data:`_DATABRICKS_APPS_HOST_SUFFIX`.
    * **the validated string is the one used** — the caller POSTs the return value, so there
      is no re-derivation between the check and the request.

    FIX round 1 (CRITICAL) — **and it must also be a URL httpx can actually use.** Two inputs
    passed every check above and still broke the request: a CRLF injection
    (``…/x\\r\\nX-Evil: 1`` — `urlparse` leaves the netloc clean and the injection rides in the
    PATH) and a soft hyphen in the host (``claims\\xad.aws…`` — invisible, survives `urlparse`,
    then fails IDNA). Both raise ``httpx.InvalidURL``, which inherits from ``Exception``
    DIRECTLY and is **not** an ``HTTPError`` subclass — so it escaped both except arms at the
    POST and surfaced as an unhandled 500. Worse than the status code: the token was already
    minted, so a live Databricks credential had been spent on a request that was never made.

    Parsing through ``httpx.URL`` HERE fixes both halves at once, because this function runs
    before any token is obtained. Two parsers rather than one is deliberate: `urlparse` is what
    the allowlist reasons about, and httpx's is what will actually build the request — agreement
    between them is the property worth having, since a value only the second one rejects is a
    value the first one mis-described.

    FIX round 2 — **``.host`` is accessed, and that access IS the check.** ``httpx.URL()`` is
    LAZY: it stores the raw authority and only runs the IDNA encode when the host is read. So
    round 1's construct-and-discard closed its two named inputs while leaving the whole CLASS
    open — ``https://xn--.aws.databricksapps.com`` (an empty punycode A-label), ``xn--a``,
    ``xn--evil`` all constructed fine, passed validation, **cost a real Databricks token**, and
    only failed when httpx later built the request. That the failure surfaced as a clean 502
    rather than a 500 was luck, not design: ``idna.IDNAError`` subclasses ``ValueError``, so the
    POST-site ``ValueError`` arm caught it — meaning that arm is LOAD-BEARING, not the
    defence-in-depth an earlier version of this comment claimed. Touching ``.host`` moves the
    encode to where the decision belongs, ahead of the mint.

    It discriminates rather than merely blocking: a legitimate internationalized label
    (``claims.ß.…``) encodes successfully and is still accepted, as are an uppercase host, an
    explicit port, and a query string — all pinned by test.
    """
    url = (raw or "").strip()
    if not url:
        raise _databricks_502("invalid_runtime_handle")
    try:
        parsed = urlparse(url)
    except ValueError:
        raise _databricks_502("invalid_runtime_handle") from None
    if parsed.scheme != "https":
        raise _databricks_502("invalid_runtime_handle")
    if "@" in (parsed.netloc or ""):
        raise _databricks_502("invalid_runtime_handle")
    host = (parsed.hostname or "").strip().lower()
    if not host.endswith(_DATABRICKS_APPS_HOST_SUFFIX):
        raise _databricks_502("invalid_runtime_handle")
    # The request builder's OWN verdict (see the note above). `.host` is NOT a redundant read:
    # httpx.URL is lazy, so accessing the host is what forces the IDNA encode to run here rather
    # than at the POST — after a token would already have been spent (fix round 2). Broad on
    # purpose: InvalidURL for a non-printable character, idna.IDNAError (a ValueError) for a bad
    # A-label, and a bare ValueError for an unroutable value — none are HTTPError subclasses.
    try:
        httpx.URL(url).host
    except Exception:  # noqa: BLE001 — InvalidURL, IDNAError, ValueError: one safe answer
        raise _databricks_502("invalid_runtime_handle") from None
    return url


def _databricks_invoke_url(agent: Agent) -> str:
    """The app URL to POST, VALIDATED (OB-4).

    `endpoint_url` wins when set — a customer whose app exposes a different route says so on
    the record — and the template's `/api/v1/agent` is the default for everything else. Both
    are validated on the same rule: a valid handle plus a hostile `endpoint_url` must not be
    the bypass, since `endpoint_url` is the value actually requested.
    """
    endpoint = (agent.endpoint_url or "").strip()
    if endpoint:
        return _validate_databricks_app_url(endpoint)
    handle = _validate_databricks_app_url(agent.runtime_handle or "")
    return f"{handle.rstrip('/')}{_DATABRICKS_DEFAULT_ROUTE}"


def _resolve_databricks_tenant(agent: Agent):
    """The agent's tenant record, or a safe 502.

    Read through the tenants-route singleton — the SAME instance the admin routes write, so a
    tenant connected a moment ago is visible here — imported lazily for the reason
    :func:`get_databricks_identity_service` documents (a module-level import is a cycle).

    The binding mode and the workspace both come from the TENANT, never from the agent record.
    `Agent.binding_mode` is client-settable and is only a copy taken at registration; trusting
    it would let a caller pick the weaker credential path on a federation tenant, which is
    precisely the silent downgrade T6 refuses to make at provisioning time.
    """
    tenant_id = (agent.tenant_id or "").strip()
    if not tenant_id:
        raise _databricks_502("tenant_unresolved")
    from api.routes.tenants import get_tenant_service

    try:
        tenant = get_tenant_service().get(tenant_id)
    except Exception:  # noqa: BLE001 — TenantError, a store fault, anything: same safe answer
        logger.warning(
            "[invoke] could not resolve the tenant for Databricks agent %s", agent.id
        )
        raise _databricks_502("tenant_unresolved") from None
    if tenant is None:
        raise _databricks_502("tenant_unresolved")
    return tenant


def _resolve_databricks_workspace(agent: Agent, tenant) -> str:
    """Which workspace hosts this agent? Returns its URL, or a safe 502.

    THE SIMPLEST CORRECT RULE, deliberately, and its limit is documented rather than hidden:

    * exactly one Databricks stage → that one (the epic's shape, and the live test's);
    * several → the stage whose `workspace_url` is a prefix of the agent's handle;
    * no match → refuse with ``workspace_stage_unresolved``.

    T6's `_resolve_stage_and_app` picks a stage by LISTING each workspace's apps, which is
    strictly better evidence — but it is private, and it costs a token mint plus a paginated
    listing per attempt, which is not a price the hot invoke path should pay to learn something
    the single-workspace case already knows. The trade is stated because it has a real edge: an
    Apps URL is `<app>-<n>.<region>.databricksapps.com` and carries NO workspace identity, so
    on a genuinely multi-workspace Databricks tenant the prefix rule cannot match and invoke
    fails closed with that code. Failing closed is the honest outcome — exchanging the caller's
    token at a workspace that does not host the app would either leak an assertion to the wrong
    workspace or produce a 502 that looks like the app's fault. When multi-workspace tenants
    become real, the fix is to resolve the stage AT PROVISIONING TIME and persist it on the
    record, not to guess here.
    """
    stages = getattr(tenant, "stages", None) or {}
    # DISTINCT workspaces, not stage entries: the tenant form requires BOTH stages
    # complete, so a single-workspace operator duplicates one workspace_url into dev and
    # prod — that is still the single-workspace case, not ambiguity (E29 livefix-5: the
    # entry count refused every form-created single-workspace tenant). Normalized the same
    # way the prefix match below reads them; dict-ordered by stage name so a refusal is
    # reproducible.
    seen: dict[str, str] = {}
    for _name, stage in sorted(stages.items(), key=lambda kv: kv[0]):
        url = (getattr(stage, "workspace_url", "") or "").strip()
        if url:
            seen.setdefault(url.rstrip("/").lower(), url)
    workspaces = list(seen.values())
    if not workspaces:
        raise _databricks_502("workspace_stage_unresolved")
    if len(workspaces) == 1:
        return workspaces[0]
    handle = (agent.runtime_handle or "").strip().rstrip("/").lower()
    for url in workspaces:
        if handle.startswith(url.rstrip("/").lower()):
            return url
    logger.warning(
        "[invoke] agent %s's handle matches none of its tenant's %d Databricks workspaces, and "
        "an Apps hostname carries no workspace identity to disambiguate on — record the bound "
        "workspace stage on the agent at provisioning time to make this resolvable",
        agent.id,
        len(workspaces),
    )
    raise _databricks_502("workspace_stage_unresolved")


def _databricks_agent_secret(agent: Agent) -> str:
    """The per-agent service-principal secret, read under T6's TRUSTED-DERIVATION rule.

    `databricks_sp_secret_arn` is client-settable (OB-2), so the ARN is only honoured when it
    is one the identity service would have written FOR THIS AGENT. That check is
    `DatabricksIdentityService._owns_secret_arn`, and it is REUSED rather than reimplemented:
    it encodes two non-obvious properties (prefix-independence, and injective boundaries at
    both ends of the agent id) that a second copy would get subtly wrong, and two disagreeing
    copies of an ownership rule mean the looser one is the bypass. Without it, a record
    registered with the TENANT's workspace-SP ARN would have this route mint a token from a
    credential the agent was never granted.

    The private reach is deliberate and bounded to these two members: this task's manifest
    covers the route, not that service, so adding a public wrapper there is out of scope. If
    the invoke path grows further needs, the right move is a public `read_agent_credential`
    on the service — not more underscores here.

    The VALUE lives in a local and goes straight into the token mint: never returned, never
    logged, never interpolated into an error (pinned by a sentinel test).
    """
    arn = (agent.databricks_sp_secret_arn or "").strip()
    client_id = (agent.databricks_sp_id or "").strip()
    if not arn or not client_id:
        raise _databricks_502("sp_credential_unavailable")
    identity = get_databricks_identity_service()
    if identity is None:
        logger.warning(
            "[invoke] no Databricks identity service is configured, so agent %s's stored "
            "service-principal credential cannot be read",
            agent.id,
        )
        raise _databricks_502("sp_credential_unavailable")
    if not identity._owns_secret_arn(agent, arn):
        # Never log the ARN — it names another agent's (or the tenant's) secret.
        logger.warning(
            "[invoke] agent %s names a Databricks secret this platform did not write for it; "
            "refusing to mint a token from it",
            agent.id,
        )
        raise _databricks_502("sp_credential_unavailable")
    try:
        secret = identity._read_secret(arn, _DATABRICKS_AGENT_SECRET_KEY)
    except Exception:  # noqa: BLE001 — ProvisioningError or a boto3 fault: same safe answer
        logger.warning(
            "[invoke] agent %s's stored Databricks credential could not be read", agent.id
        )
        raise _databricks_502("sp_credential_unavailable") from None
    if not secret:
        raise _databricks_502("sp_credential_unavailable")
    return secret


async def _databricks_token(agent: Agent, tenant, workspace_url: str, obo_token: str) -> str:
    """The Databricks token to bear, by the TENANT's binding mode (design §3).

    * **federation** — RFC 8693 exchange of the OBO token at the workspace, so the app and
      Unity Catalog see the REAL caller. This is the governed path and the reason the epic
      exists.
    * **invoke_unavailable** — REFUSED with its own code (E29/T14a, design §3B). The tenant was
      probed and cannot federate, so there is no honest identity to invoke with; the message
      names the two grants federation needs rather than leaving the operator to guess.
    * **sp_secret** — DORMANT. The OBO token would be DISCARDED here (it has already done its
      job: the exchange upstream is what refused an unassigned caller) and a client-credentials
      token minted from the agent's own stored secret — which attributes every call to that
      service principal, so the Databricks audit trail stops at AGP's boundary. Reachable only
      behind ``settings.DATABRICKS_ALLOW_SP_SECRET_BINDING``; with the flag off (the default) it
      is refused as ``sp_secret_disabled``.

    An unrecognised or empty mode is REFUSED, not defaulted. A default would pick a mode, and
    the modes differ in what the audit log can prove — the same reason T6 refuses to downgrade a
    federation tenant silently.
    """
    mode = (getattr(tenant, "binding_mode", "") or "").strip()
    databricks = get_databricks_workspace_service()

    if mode == _MODE_FEDERATION:
        try:
            return await databricks.exchange_federated_token(workspace_url, obo_token)
        except Exception as err:  # noqa: BLE001 — DatabricksError or transport: one safe code
            logger.warning(
                "[invoke] the federated-token exchange failed for agent %s (%s)",
                agent.id,
                getattr(err, "kind", None) or type(err).__name__,
            )
            raise _databricks_502("federation_exchange_failed") from None

    if mode == _MODE_INVOKE_UNAVAILABLE:
        # E29/T14a (design §3B) — ITS OWN CODE, not the `binding_mode_unresolved` catch-all.
        # Those are two different statements: `binding_mode_unresolved` means "this tenant was
        # never probed", while this means "it WAS probed and cannot federate". An operator who
        # reads the first goes looking at the tenant record; only the second tells them the fix
        # is on their Databricks account. See `_DATABRICKS_ACTIONABLE` for the sentence.
        logger.warning(
            "[invoke] agent %s's tenant cannot federate, so AGP will not invoke it "
            "(federation_unavailable)",
            agent.id,
        )
        raise _databricks_502("federation_unavailable")

    if mode == _MODE_SP_SECRET and not settings.DATABRICKS_ALLOW_SP_SECRET_BINDING:
        # The dormant binding is not CONSUMABLE on a default deployment (design §3B), and the
        # refusal comes before the credential is read: a token minted here would attribute the
        # call to a service principal, which is exactly the attribution this epic removed.
        logger.warning(
            "[invoke] agent %s carries the dormant sp_secret binding and this deployment does "
            "not allow it (sp_secret_disabled)",
            agent.id,
        )
        raise _databricks_502("sp_secret_disabled")

    if mode == _MODE_SP_SECRET:
        # Read the credential FIRST: a refusal here must not have contacted Databricks.
        secret = _databricks_agent_secret(agent)
        try:
            return await databricks.mint_m2m_token(
                workspace_url, str(agent.databricks_sp_id or ""), secret
            )
        except Exception as err:  # noqa: BLE001 — same reasoning as above
            logger.warning(
                "[invoke] the service-principal token mint failed for agent %s (%s)",
                agent.id,
                getattr(err, "kind", None) or type(err).__name__,
            )
            raise _databricks_502("token_mint_failed") from None

    logger.warning(
        "[invoke] agent %s's tenant carries no usable Databricks binding mode", agent.id
    )
    raise _databricks_502("binding_mode_unresolved")


async def _invoke_databricks(
    agent: Agent, req: "InvokeRequest", principal: Principal, obo_token: str
):
    """Steps 4-5 for a Databricks-governed agent (contract C-5). Returns the SAME envelope.

    ORDER IS THE SECURITY PROPERTY, and it is the reverse of what convenience would suggest:
    the tenant, the workspace and the URL are all resolved and VALIDATED before a Databricks
    token is obtained. So a hostile `runtime_handle` is refused with no credential in flight,
    and a mis-keyed record is refused before AGP spends a token mint — pinned by tests that
    assert the Databricks fake was never touched, and by
    `test_invoke_databricks_ob4_validation_precedes_the_token`, which pins the SEQUENCE rather
    than inferring it from an absence. Fix round 1 made that ordering load-bearing for a second
    reason: OB-4 now also parses through httpx's own URL builder, so a value only httpx rejects
    is caught here rather than at the POST — after a token would already have been spent.

    The response contract is the AgentCore branch's, through the same `_extract_sse_text`: the
    frontend's InvokePanel reads one shape, and a second shape here would be a second renderer.
    The AgentCore session header is NOT sent — it is AgentCore's own protocol element and means
    nothing to a Databricks app.
    """
    tenant = _resolve_databricks_tenant(agent)
    workspace_url = _resolve_databricks_workspace(agent, tenant)
    url = _databricks_invoke_url(agent)

    db_token = await _databricks_token(agent, tenant, workspace_url, obo_token)

    headers = {
        "Authorization": f"Bearer {db_token}",
        "Content-Type": "application/json",
    }
    # The SAME body contract as AgentCore (`user_oid` may be None; the agent guards for it).
    invoke_body = {"prompt": req.prompt, "user_oid": principal.oid}

    try:
        # follow_redirects=False is EXPLICIT (fix round 1), even though it is httpx's default.
        # It carries OB-4's whole guarantee: the allowlist is enforced on the URL AGP requests,
        # so if redirects were followed, a validated `*.databricksapps.com` host answering
        # `302 Location: https://attacker.example` would have httpx re-send this request —
        # Authorization header included — to a host that was never validated. A property this
        # load-bearing must not rest on an upstream default that a future version could flip.
        async with httpx.AsyncClient(timeout=120.0, follow_redirects=False) as client:
            app_resp = await client.post(url, headers=headers, json=invoke_body)
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="agent invocation timed out")
    except (httpx.HTTPError, httpx.InvalidURL, ValueError):
        # `httpx.InvalidURL` and a bare `ValueError` are NOT HTTPError subclasses (the same trap
        # `databricks_workspace_service._request` documents), so catching HTTPError alone let a
        # malformed URL escape as an unhandled 500.
        #
        # NOT merely defence in depth — an earlier version of this comment claimed that and was
        # wrong (fix round 2). Between rounds this arm was the ONLY thing turning an
        # IDNA-invalid host into a 502 instead of a 500, because `idna.IDNAError` subclasses
        # `ValueError` and OB-4's construct-and-discard had not yet forced the encode. OB-4 now
        # touches `.host`, so the URL class is refused before the mint — but this arm stays
        # because the harm it bounds (an unhandled 500 from a lazily-validated URL) is exactly
        # the kind that reappears when someone "simplifies" a validator upstream.
        raise HTTPException(status_code=502, detail="failed to reach the agent")

    # The app refusing the DATABRICKS token is distinct from the OBO 403 (which means the user
    # is not assigned) — same literal the AgentCore branch uses, because it is the same fact.
    if app_resp.status_code in (401, 403):
        raise HTTPException(status_code=502, detail="agent rejected the token")
    if not (200 <= app_resp.status_code < 300):
        raise HTTPException(status_code=502, detail="agent returned an error response")

    content_type = app_resp.headers.get("content-type", "")
    if "text/event-stream" in content_type:
        return {"response": _extract_sse_text(app_resp.text)}
    try:
        return {"response": app_resp.json()}
    except (ValueError, TypeError):
        return {"response": app_resp.text}


@router.post("/{agent_id}/invoke")
async def invoke_agent(
    agent_id: str,
    req: InvokeRequest,
    request: Request,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.VIEWER)),
    stage: Optional[str] = Query(default=None),
):
    """Invoke an AgentCore agent via OBO → httpx bearer-POST (research §3).

    WHICH RUNTIME THIS REACHES (E36/T2, closing D-A4 defect 2). Since E28A/T1b an agent owns one
    runtime PER STAGE, so "invoke the agent" is an incomplete instruction. ``?stage=`` is OPTIONAL
    and ADDITIVE, on ``runtime_status``' idiom below — the same parameter, the same resolver
    (``agent.runtime_arns()``), so the invoke surface and the status surface cannot disagree about
    which runtime a stage names:

      - GIVEN a stage, THAT stage's runtime is invoked. A stage the agent owns no runtime for is
        refused with **404 ``unknown stage``** — never a fall-through to another stage's runtime,
        which would answer a question the caller did not ask while looking like an answer to the
        one they did. The refusal precedes the OBO exchange, so it costs no Entra round-trip.
      - OMITTED, the behaviour is the pre-E36 one BYTE FOR BYTE: the runtime ``agent.agent_arn``
        names, which by contract C-A2 is whichever stage deployed last. Existing callers are not
        silently re-pointed, and this route still does NOT invent a default stage — picking one
        (``dev``, or the first map key) would look deliberate while still being arbitrary.
      - A LEGACY (scalar-only) record asked for an explicit stage gets the same 404, because
        ``runtime_arns()`` keys its one runtime ``UNKNOWN_STAGE``: the record genuinely cannot
        attribute it, and captioning it with the requested stage would be a fabrication.

    Free-form (D8), exactly like the two sibling reads: a tenant's stage set is open, so the value
    is never validated against a conventional stage name — it is looked up in the agent's own map.

    VIEWER-gated (invoking is not a governance mutation). Flow:
      1. resolve the agent; 404 if missing OR not tenant-visible; 409 if not
         provisioned or not agentcore.
      2. read the RAW inbound bearer token (the SAME string current_principal
         validated — precedent: /users/me) for the OBO assertion.
      3. OBO-exchange it for an agent-audience token (NotAssignedError→403,
         OboConfigError→500).
      4. httpx bearer-POST the runtime (NOT boto3/SigV4 — JWT-inbound runtimes reject
         SigV4). The runtime STREAMS SSE; buffer it + extract the terminal message.
      5. runtime 401/403 → 502 ("agent rejected the token"); timeout → 504.

    E29/T7 — TWO PLATFORMS, ONE ENFORCEMENT POINT (contract C-5, design §3). Steps 1-3 above
    are now PLATFORM-NEUTRAL and steps 4-5 dispatch: an AgentCore agent continues below
    unchanged, and a Databricks-governed one is handed to :func:`_invoke_databricks`. The
    dispatch deliberately sits AFTER the OBO exchange rather than at the top of the handler,
    which makes "the Entra grant is checked before any platform is contacted" a STRUCTURAL
    property of this function instead of a convention two branches have to keep agreeing on.
    That matters most in Databricks ``sp_secret`` mode, where the OBO token is discarded
    immediately afterwards: the exchange is kept solely because it is the enforcement point,
    and a branch that skipped it would let any caller who can reach this route borrow the
    agent's service principal.
    """
    agent = await _load_visible_agent(agent_id, ctx)
    # Strict allowlist: pending / failed / none are all blocked before any OBO/runtime
    # call. An agent with no runtime on EITHER platform is also blocked — there is nothing
    # to invoke. The two gates are mutually exclusive by construction (they demand different
    # `platform` values), so this is a dispatch, not a race.
    if agent.identity_status != "provisioned" or not (
        is_agentcore_agent(agent) or is_databricks_governed_agent(agent)
    ):
        raise HTTPException(status_code=409, detail="agent identity is not provisioned")

    # (1b) WHICH runtime — resolved HERE, before any OBO/runtime call, so an unresolvable stage
    # is refused for free. `runtime_arns()` is the ONE resolver (models/agent.py), the same one
    # `runtime_status` reads through; the stage-less branch stays the scalar, verbatim.
    if stage is None:
        agent_arn = agent.agent_arn or ""
    else:
        staged_arn = agent.runtime_arns().get(stage)
        if not staged_arn:
            raise HTTPException(status_code=404, detail="unknown stage")
        agent_arn = staged_arn

    # The raw inbound token = the Authorization header value minus "Bearer " — the SAME
    # string current_principal already validated (precedent: /users/me re-reads it).
    auth_header = request.headers.get("authorization", "")
    user_token = auth_header.split(" ", 1)[1] if " " in auth_header else ""

    # (3) OBO exchange — Entra's token endpoint is the real enforcement point.
    try:
        obo_token = await get_graph_service().obo_exchange(
            user_token, agent.entra_app_audience
        )
    except NotAssignedError:
        who = principal.email or principal.oid or "the caller"
        raise HTTPException(
            status_code=403, detail=f"{who} is not assigned to this agent"
        )
    except OboConfigError:
        # A backend/consent misconfig (CRITIQUE-FIX-D) — NOT a user-permission 403 and
        # NOT a runtime 502. Surface a re-provision hint.
        raise HTTPException(
            status_code=500,
            detail="agent identity is misconfigured — re-provision",
        )

    # (4a) PLATFORM DISPATCH (E29/T7). Everything above was neutral; everything below is
    # AgentCore's, byte for byte. A Databricks-governed agent leaves here.
    if is_databricks_governed_agent(agent):
        return await _invoke_databricks(agent, req, principal, obo_token)

    # (4) httpx bearer-POST the runtime. RID-not-ARN for parsing, but the URL path
    # carries the URL-encoded FULL ARN (research §1/§3). Region = ARN segment 3.
    # A corrupt/short ARN would IndexError here → guard it into a clean 502 rather
    # than an unhandled 500 (low-likelihood behind the 409 provisioned-gate, but
    # cheap to harden). `agent_arn` was resolved at (1b) — scalar, or the requested stage's.
    arn_parts = agent_arn.split(":")
    if len(arn_parts) < 4 or not arn_parts[3]:
        raise HTTPException(status_code=502, detail="agent ARN is malformed")
    region = arn_parts[3]
    url = (
        f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/"
        f"{quote(agent_arn, safe='')}/invocations?qualifier=DEFAULT"
    )
    session_id = uuid4().hex + uuid4().hex  # >= 33 chars (documented min)
    headers = {
        "Authorization": f"Bearer {obo_token}",
        "Content-Type": "application/json",
        "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": session_id,
    }

    # Forward the invoker's Entra oid in the runtime payload so the deployed agent can
    # attribute telemetry (e.g. Langfuse) per-user WITHOUT decoding the inbound token
    # (AgentCore's edge authorizer consumes the inbound Authorization header). The
    # backend already validated the user and holds `principal.oid` in plain form here;
    # this is the authoritative source. `principal.oid` MAY be None — send it anyway
    # (the agent guards for None). `user_oid` is the agreed contract key the agent reads.
    invoke_body = {"prompt": req.prompt, "user_oid": principal.oid}

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            rt_resp = await client.post(url, headers=headers, json=invoke_body)
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="agent invocation timed out")
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail="failed to reach the agent")

    # (5) runtime auth rejection (live-confirmed: 403 + OAuth-parse body) → 502, distinct
    # from the OBO 403. A wrong-aud (valid-format) token is likewise a runtime 401/403.
    if rt_resp.status_code in (401, 403):
        raise HTTPException(status_code=502, detail="agent rejected the token")
    if not (200 <= rt_resp.status_code < 300):
        raise HTTPException(
            status_code=502, detail="agent returned an error response"
        )

    # The runtime STREAMS SSE (text/event-stream) — the EXPECTED path; tolerate a
    # non-streaming application/json agent as a fallback.
    content_type = rt_resp.headers.get("content-type", "")
    if "text/event-stream" in content_type:
        return {"response": _extract_sse_text(rt_resp.text)}
    try:
        return {"response": rt_resp.json()}
    except (ValueError, TypeError):
        return {"response": rt_resp.text}


# --- runtime status (Epic 28/T5 — design D9, contract C2) --------------------

@router.get("/{agent_id}/runtime", response_model=RuntimeStatus)
async def get_agent_runtime_status(
    agent_id: str,
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.VIEWER)),
    stage: Optional[str] = Query(default=None),
):
    """Live AgentCore Runtime status for one agent (E28/T5).

    ``?stage=`` is OPTIONAL and ADDITIVE (E28A/T1). Omitted — the pre-E28A behaviour, byte for
    byte — it probes the runtime ``agent_arn`` names ("whichever stage deployed last") and now
    REPORTS which stage that was whenever the record's per-stage map can attribute it. Given a
    stage, it probes THAT stage's runtime, and a stage the agent has no runtime for comes back
    ``not_deployed`` rather than falling through to another stage's reading. A legacy
    (scalar-only) record still answers ``stage="unknown"``, because it genuinely cannot
    attribute its one runtime — the frontend reads any other value as per-stage evidence.
    Mirrors ``/deployments``' ``?stage=``, so the two sibling reads take the same parameter.

    Tenant + VIEWER gated, on EXACTLY the gate the per-agent ``/metrics`` and ``/traces``
    reads use: the shared ``_load_visible_agent`` (so a foreign/unknown agent gets the
    byte-identical "Agent not found" — never a leak), then the read. Deliberately NO
    project-role gate, for the reason stated at the top of this module: reads are
    tenant-wide by design and only MUTATIONS carry the per-project threshold. This is the
    same KIND of read as ``/metrics``, so giving it a different gate would make two
    sibling read surfaces disagree — and whichever was looser would be the bypass.

    The probe is SYNC boto3 with a network round-trip, so it is dispatched OFF the event
    loop (never block the uvicorn loop). It NEVER raises: an unreachable or denying control
    plane comes back as ``status="unknown"`` with a safe hint, which is why this handler has
    no error mapping — 200-with-unknown is the honest answer, and a 5xx here would blank the
    fleet view over a throttle. ``unknown`` is distinct from ``failed`` by contract.
    """
    agent = await _load_visible_agent(agent_id, ctx)
    status = await anyio.to_thread.run_sync(
        get_identity_service().runtime_status, agent, stage
    )
    # E36/T12 — RECONCILE-ON-READ, an explicitly accepted write on a GET. A runtime REPLACEMENT
    # (our pipeline's, or a customer's own `agentcore launch` for a registered-external agent)
    # drops the backend-injected `MCP_SERVERS` and the platform gets NO signal, so the record
    # keeps asserting a wiring the runtime no longer has. This read already fetched the live env,
    # which makes the detection free; the heal only fires when the probe REACHED the runtime and
    # found it grant-carrying but MCP_SERVERS-less (a degraded probe hands over `None` and heals
    # nothing), and it never raises, so the read stays 200-or-a-status.
    #
    # It also never BLOCKS this request on the runtime coming back READY, and one agent gets at
    # most one heal in flight — the repository page reads this endpoint 1+N times at once.
    #
    # The env is passed INTERNALLY and never serialized: `response_model=RuntimeStatus` has no
    # such field, and this is the VIEWER-gated read surface.
    await reconcile_runtime_mcp_env(agent, status.environment_variables)
    return status


# --- deployment history (Epic 28/T11 — design D7, contract C2) ---------------

@router.get("/{agent_id}/deployments", response_model=List[Deployment])
async def list_agent_deployments(
    agent_id: str,
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.VIEWER)),
    stage: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
):
    """One agent's append-only delivery history, NEWEST FIRST (E28/T11).

    T3 built the store and T4 built the appends; no task had mounted the read, so C2's pinned
    route did not exist. This is it.

    THE GATE IS ``/runtime``'S, VERBATIM — the same two dependencies in the same order, then
    the shared ``_load_visible_agent``. These are two sibling READS of the same resource, and
    two sibling reads must not carry different guards: whichever is looser becomes the bypass
    (D3 — guards compose on one handler; never clone a route to change a guard). A test asserts
    the two handlers' dependency lists are identical objects rather than trusting the eye.

    ``repo_id`` IS RESOLVED SERVER-SIDE and is deliberately not a query param. The store is
    keyed by ``repo_id`` while the caller holds an ``agent_id``; accepting a ``repo_id`` would
    let a caller read ANOTHER repo's history under an agent they happen to be able to see —
    the visibility gate above is on the agent, not on the repo. ``find_repository_by_agent_id``
    is the existing resolver (the builds route's E25/I1 token↔agent binding uses it for the
    same 1:1 relationship); the buildspec resolves the same edge through the ``agent_id-index``
    GSI, which is the DDB-side equivalent.

    AN AGENT WITH NO REPOSITORY RETURNS ``[]``. A directly-registered (or pre-E20) agent owns
    no repo and therefore has no deployment history — that is a STATE, not an error, and a 404
    here would be indistinguishable from the visibility 404 above.

    ``stage`` IS FREE-FORM (D8) and is NEVER validated against a dev/prod literal: a tenant's
    stage set is open, so a tenant whose only stage is ``uat`` must be able to filter by it.
    Omitted ⇒ ``None`` ⇒ the store's cross-stage merge (its ``if stage:`` branch), never a
    default literal. ``limit`` mirrors ``/traces``' ``Query(default=50, ge=1, le=100)``.

    NO ERROR MAPPING, deliberately. ``list_deployments`` degrades to ``[]`` on an unreachable
    or malformed store and never raises (hardened over three T3/T2 rounds), so a 200 with an
    empty history is the honest answer — mapping it to a 5xx would blank a repo page over a
    DynamoDB throttle, the same reasoning ``/runtime`` states above."""
    from api.routes.projects import get_project_service

    agent = await _load_visible_agent(agent_id, ctx)
    svc = get_project_service()
    repo = await anyio.to_thread.run_sync(svc.find_repository_by_agent_id, agent.id)
    if repo is None:
        return []
    return await anyio.to_thread.run_sync(
        functools.partial(svc.list_deployments, repo.id, stage=stage, limit=limit)
    )


# --- observability (Epic 26/T6 — per-agent Langfuse metrics + traces) --------

@router.get("/{agent_id}/metrics")
async def get_agent_metrics(
    agent_id: str,
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.VIEWER)),
    date_from: Optional[date] = Query(default=None),
    date_to: Optional[date] = Query(default=None),
):
    """Per-agent Langfuse cost/token/trace metrics over ``[date_from, date_to]`` (C4).

    VIEWER-gated read. Visibility-gated 404 via the SHARED ``_load_visible_agent`` (a
    foreign/unknown agent → the byte-identical "Agent not found" — never a leak), THEN
    the T5 ``get_agent_metrics`` (which degrades an unprovisioned/failed read to zeroed
    metrics, never raising). ``date_from``/``date_to`` default to the trailing 30-day
    window when omitted. The Langfuse key VALUE never touches this response."""
    from api.routes.observability import get_metrics_service

    agent = await _load_visible_agent(agent_id, ctx)
    date_to = date_to or date.today()
    date_from = date_from or (date_to - timedelta(days=30))
    return await get_metrics_service().get_agent_metrics(agent, date_from, date_to)


@router.get("/{agent_id}/traces")
async def get_agent_traces(
    agent_id: str,
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.VIEWER)),
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=50, ge=1, le=100),
):
    """Paged per-agent trace list → ``{"data": [TraceRow], "total": int}`` (C4).

    VIEWER-gated read; same visibility-gated 404 as ``/metrics``. The T5
    ``get_agent_traces`` degrades an unprovisioned/failed read to an empty page and never
    raises. No key VALUE is surfaced."""
    from api.routes.observability import get_metrics_service

    agent = await _load_visible_agent(agent_id, ctx)
    return await get_metrics_service().get_agent_traces(agent, page=page, limit=limit)
