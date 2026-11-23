"""MCP server registry routes: CRUD, lifecycle, publish, identity re-provisioning, tool refresh.

Backed by ``McpServerRegistryService``; a structural twin of ``routes/agents.py``. RBAC: list and
get at VIEWER; create, update, delete, submit, publish, reprovision and refresh-tools at OPERATOR;
transitions at ADMIN. ``created_by`` and the default owner come from the validated principal.

Multi-tenancy: ``tenant_ctx`` gates every detail, mutation, lifecycle and identity route through
``_load_visible_mcp_server`` before any side effect, and post-filters ``list``. ``shared`` grants
cross-tenant READ visibility only; every write passes ``for_write=True`` so a foreign shared MCP is
404 on a write path. Setting or clearing ``shared`` is ADMIN-only.
"""

import logging
from typing import List, Optional

import anyio.to_thread
from fastapi import APIRouter, BackgroundTasks, Query
from fastapi import Depends as RBACDepends

from core.config import settings
from core.rbac import Principal, Role, current_principal, require_role
from models.base import WireModel
from models.mcp_server import (
    IdentityStatus,
    Kind,
    LifecycleState,
    McpServer,
    McpServerCreate,
    McpServerUpdate,
)
from models.transition import TransitionRequest
from services.mcp_identity_service import McpIdentityService, should_provision_mcp
from services.mcp_server_service import (
    McpServerNotFoundError,
    McpServerRegistryService,
    McpServerRequestError,
)
from services.mcp_teardown import teardown_mcp_identity
from services.tenant_resolver import TenantContext, require_member, visible

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/mcp-servers", tags=["mcp-servers"])

_svc: Optional[McpServerRegistryService] = None
_identity_svc: Optional[McpIdentityService] = None


def get_service() -> McpServerRegistryService:
    """Lazy ``McpServerRegistryService`` singleton, addressed by name with ``MCP_REGISTRY_ID`` as
    an explicit override (see the registry writer's ``registry_id``)."""
    global _svc
    if _svc is None:
        _svc = McpServerRegistryService(
            registry_id=getattr(settings, "MCP_REGISTRY_ID", ""),
            registry_name=getattr(settings, "MCP_REGISTRY_NAME", ""),
            region=getattr(settings, "MCP_REGISTRY_REGION", "us-east-1"),
        )
    return _svc


def get_mcp_identity_service() -> McpIdentityService:
    """Lazy ``McpIdentityService`` singleton over the shared Graph service (owned by
    ``routes/mcp_server_grants``) and the registry singleton."""
    global _identity_svc
    if _identity_svc is None:
        from api.routes.mcp_server_grants import get_mcp_graph_service

        _identity_svc = McpIdentityService(
            graph=get_mcp_graph_service(),
            registry=get_service(),
            tenant_id=settings.ENTRA_TENANT_ID,
            login_base=settings.ENTRA_LOGIN_BASE,
            region=getattr(settings, "MCP_REGISTRY_REGION", "") or "us-east-1",
        )
    return _identity_svc


async def get_tenant_ctx(
    principal: Principal = RBACDepends(current_principal),
) -> TenantContext:
    """The per-request tenant context, through the one resolver singleton ``users.py`` owns
    (tests patch ``api.routes.users._tenant_resolver``). Imported lazily to avoid an import cycle."""
    from api.routes.users import get_tenant_ctx as _users_get_tenant_ctx

    return await _users_get_tenant_ctx(principal)


async def _load_visible_mcp_server(
    mcp_server_id: str, ctx: TenantContext, *, for_write: bool = False
) -> McpServer:
    """Load an MCP server by id and gate it on tenant visibility before any side effect.

    A missing and a not-visible MCP raise the same 404 literal. ``shared`` grants cross-tenant
    READ visibility only: ``for_write=True`` drops that bypass, so a foreign shared MCP is 404 on
    every write path.
    """
    mcp_server = get_service().get(mcp_server_id)
    shared = bool(mcp_server and mcp_server.shared and not for_write)
    if not mcp_server or not visible(ctx, mcp_server.tenant_id, shared=shared):
        raise McpServerNotFoundError(f"MCP server {mcp_server_id} is missing or not visible to the caller")
    return mcp_server


class PublishToggle(WireModel):
    """Body for the cross-tenant publish toggle."""

    published: bool


_REPO_MANAGED_FIELDS = ("name", "kind", "gateway_arn", "runtime_arn", "endpoint_url")


def _reject_shared_if_not_admin(shared: Optional[bool], principal: Principal) -> None:
    """``shared`` is settable only by ADMIN, on create and on update.

    ``None`` means the payload left ``shared`` out and is always allowed; any explicit value,
    ``True`` or ``False``, from a non-admin is rejected, so an OPERATOR can no more un-share a
    platform-shared MCP than share one.
    """
    if shared is not None and principal.role < Role.ADMIN:
        raise McpServerRequestError(f"{principal.role.name} may not set shared", kind="shared_admin_only")


@router.post("", response_model=McpServer, status_code=201)
async def create_mcp_server(
    req: McpServerCreate,
    background_tasks: BackgroundTasks,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    """Create the record, then schedule identity provisioning for an AgentCore gateway or runtime
    MCP.

    The tenant must exist (the tenant service's own 404) and, for a non-global caller, be one of
    the caller's memberships (403: no resource exists yet). The creator owns by default when the
    wizard leaves the owner blank. ``McpServerRegistryService.create`` does not stamp
    ``identity_status``, so the route sets ``pending`` and persists it before the 201 is sent,
    then runs ``provision`` as a background task; the service turns any provisioning failure into
    ``identity_status='failed'``. A standard MCP gets no provisioning.
    """
    from api.routes.tenants import get_tenant_service

    get_tenant_service().get(req.tenant_id)
    require_member(ctx, req.tenant_id)

    _reject_shared_if_not_admin(True if req.shared else None, principal)

    if not req.owner_email:
        req.owner_email = principal.email
    if not req.owner_oid:
        req.owner_oid = principal.oid

    svc = get_service()
    mcp = svc.create(req, created_by=principal.email or principal.oid)

    if should_provision_mcp(mcp):
        logger.info(
            "[mcp_servers] scheduling background provisioning for MCP %s (kind=%s)",
            mcp.id,
            mcp.kind,
        )
        mcp.identity_status = IdentityStatus.PENDING
        await anyio.to_thread.run_sync(svc.persist_identity, mcp)
        background_tasks.add_task(get_mcp_identity_service().provision, mcp)
    else:
        logger.debug("[mcp_servers] no provisioning for standard MCP %s", mcp.id)

    return mcp


@router.get("", response_model=List[McpServer])
async def list_mcp_servers(
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.VIEWER)),
    lifecycle_state: Optional[LifecycleState] = Query(default=None),
    kind: Optional[Kind] = Query(default=None),
    owner_oid: Optional[str] = Query(default=None),
    business_unit: Optional[str] = Query(default=None),
    region: Optional[str] = Query(default=None),
):
    """List MCP servers, post-filtered to the caller's tenants (the registry has no tenant
    concept); ``shared`` servers pass for every caller."""
    svc = get_service()
    records = svc.list(
        lifecycle_state=lifecycle_state,
        kind=kind,
        owner_oid=owner_oid,
        business_unit=business_unit,
        region=region,
    )
    return [r for r in records if visible(ctx, r.tenant_id, shared=r.shared)]


@router.get("/{mcp_server_id}", response_model=McpServer)
async def get_mcp_server(
    mcp_server_id: str,
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.VIEWER)),
):
    return await _load_visible_mcp_server(mcp_server_id, ctx)


@router.put("/{mcp_server_id}", response_model=McpServer)
async def update_mcp_server(
    mcp_server_id: str,
    req: McpServerUpdate,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    """Update the user-editable fields.

    On a repository-owned record the build owns the name, the kind and the handle fields
    (``_REPO_MANAGED_FIELDS``): an unchanged value is accepted because the form re-sends the whole
    record, an actual change is refused with 409.
    """
    existing = await _load_visible_mcp_server(mcp_server_id, ctx, for_write=True)
    if existing.is_repo_owned and any(
        getattr(req, field) is not None and getattr(req, field) != getattr(existing, field)
        for field in _REPO_MANAGED_FIELDS
    ):
        raise McpServerRequestError(
            f"MCP server {mcp_server_id} handles are repository-owned", kind="repo_managed"
        )
    _reject_shared_if_not_admin(req.shared, principal)

    mcp_server = get_service().update(mcp_server_id, req)
    if not mcp_server:
        raise McpServerNotFoundError(f"MCP server {mcp_server_id} vanished before the update")
    return mcp_server


@router.delete("/{mcp_server_id}", response_model=McpServer)
async def delete_mcp_server(
    mcp_server_id: str,
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    """Tear down the MCP's platform-managed identity, then delete the registry record.

    The cascade runs first because every id it needs lives on the record. The record is deleted
    even when a teardown leg fails, so a failure logs the Entra app id and the Cedar engine id as
    the only trail for cleaning them up by hand. Order, the fail-closed property and the
    per-resource report live in ``services.mcp_teardown.teardown_mcp_identity``. A
    repository-owned record is deleted through its repository, which owns the gateway too.
    """
    mcp = await _load_visible_mcp_server(mcp_server_id, ctx, for_write=True)
    if mcp.is_repo_owned:
        raise McpServerRequestError(
            f"MCP server {mcp_server_id} is deleted with its repository", kind="delete_through_repository"
        )
    items = await teardown_mcp_identity(mcp)
    failed = [i.item for i in items if i.outcome == "failed"]
    if failed:
        logger.warning(
            "[teardown] MCP %s deleted with %s not torn down — clean up by hand: "
            "entra_app_id=%s cedar_policy_engine_id=%s",
            mcp_server_id,
            ", ".join(failed),
            mcp.entra_app_id,
            mcp.cedar_policy_engine_id,
        )
    mcp_server = get_service().delete(mcp_server_id)
    if not mcp_server:
        raise McpServerNotFoundError(f"MCP server {mcp_server_id} vanished before the delete")
    return mcp_server


@router.post("/{mcp_server_id}/submit", response_model=McpServer)
async def submit_mcp_server(
    mcp_server_id: str,
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    await _load_visible_mcp_server(mcp_server_id, ctx, for_write=True)
    mcp_server = get_service().submit_for_approval(mcp_server_id)
    if not mcp_server:
        raise McpServerNotFoundError(f"MCP server {mcp_server_id} vanished before the submit")
    return mcp_server


@router.post("/{mcp_server_id}/transitions", response_model=McpServer)
async def transition_mcp_server(
    mcp_server_id: str,
    req: TransitionRequest,
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.ADMIN)),
):
    await _load_visible_mcp_server(mcp_server_id, ctx, for_write=True)
    mcp_server = get_service().transition(mcp_server_id, req.action, req.reason)
    if not mcp_server:
        raise McpServerNotFoundError(f"MCP server {mcp_server_id} vanished before the transition")
    return mcp_server


@router.put("/{mcp_server_id}/publish", response_model=McpServer)
async def publish_mcp_server(
    mcp_server_id: str,
    req: PublishToggle,
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    """Flip the cross-tenant publish flag: a platform write that re-asserts an APPROVED record,
    not a user content edit. The write polls the registry, so it runs off the event loop."""
    await _load_visible_mcp_server(mcp_server_id, ctx, for_write=True)
    mcp_server = await anyio.to_thread.run_sync(get_service().set_published, mcp_server_id, req.published)
    if not mcp_server:
        raise McpServerNotFoundError(f"MCP server {mcp_server_id} vanished before the publish toggle")
    return mcp_server


@router.post("/{mcp_server_id}/reprovision", response_model=McpServer, status_code=202)
async def reprovision_mcp_server(
    mcp_server_id: str,
    background_tasks: BackgroundTasks,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    """Re-run identity provisioning for an AgentCore gateway or runtime MCP in any state.

    Provisioning is idempotent and resumable, so this recovers a ``failed`` or stranded
    ``pending`` MCP and re-configures a live one. ``identity_status`` is set to ``pending`` and
    persisted before ``provision`` is scheduled, so the response reflects the in-flight work. A
    repository-built gateway is re-provisioned by rebuilding the repository (Terraform owns its
    authorizer); a standard MCP has nothing to provision.
    """
    mcp = await _load_visible_mcp_server(mcp_server_id, ctx, for_write=True)
    if mcp.is_repo_owned:
        raise McpServerRequestError(
            f"MCP server {mcp_server_id} is provisioned by its repository's build",
            kind="rebuild_through_repository",
        )
    svc = get_service()
    if not should_provision_mcp(mcp):
        raise McpServerRequestError(
            f"MCP server {mcp_server_id} has no identity to provision", kind="not_provisionable"
        )

    logger.info("[mcp_servers] re-provisioning MCP %s", mcp_server_id)
    mcp.identity_status = IdentityStatus.PENDING
    await anyio.to_thread.run_sync(svc.persist_identity, mcp)
    background_tasks.add_task(get_mcp_identity_service().provision, mcp)
    return mcp


@router.post("/{mcp_server_id}/refresh-tools", response_model=McpServer, status_code=200)
async def refresh_mcp_tools(
    mcp_server_id: str,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    """Re-read a gateway's tools from the control plane and persist them, synchronously.

    Touches only the tools: the token-less native read works on a locked gateway, and a flaky
    read is best-effort inside the service (existing tools are never wiped). Gateway-only, because
    runtime and standard MCPs have no native target read.
    """
    logger.info("[mcp_servers] refresh-tools requested for MCP %s", mcp_server_id)
    mcp = await _load_visible_mcp_server(mcp_server_id, ctx, for_write=True)
    if mcp.kind != Kind.GATEWAY:
        raise McpServerRequestError(
            f"MCP server {mcp_server_id} is a {mcp.kind.value} MCP", kind="tools_gateway_only"
        )
    return await get_mcp_identity_service().refresh_tools(mcp)
