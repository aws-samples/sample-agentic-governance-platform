"""Tenant admin API: admin-gated CRUD, the re-probe verb and platform discovery.

Every endpoint, the list included, is ``require_role(Role.ADMIN)``; members get their tenants
from ``/users/me``. ``created_by`` is the validated principal's identity, never the body.
``TenantError`` renders through its table. The lazy ``_svc`` singleton and the module-level
reference-check and catalog hooks are what tests patch, so nothing here reaches live AWS.

DELETE runs the reference check here, not in the service: a tenant is removed only once no
agent, MCP server or project names it, and a source that cannot be read fails closed to 409.

``POST``, ``PUT`` and the probe are sync handlers, which FastAPI runs in a threadpool: the
service drives the Databricks capability probe through ``asyncio.run``, which raises under a
running event loop.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Query, Response
from fastapi import Depends as RBACDepends

from core.config import settings
from core.rbac import Principal, Role, current_principal, require_role
from models.tenant import Tenant, TenantCreate, TenantPlatform, TenantUpdate, TenantView
from services.runtime_catalog import (
    AgentCoreCatalog,
    DatabricksCatalog,
    DiscoveredAgents,
    RuntimeCatalog,
    discover_agents,
    mark_already_registered,
)
from services.tenant_service import TenantError, TenantService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/tenants", tags=["tenants"])

_svc: Optional[TenantService] = None


def get_tenant_service() -> TenantService:
    global _svc
    if _svc is None:
        _svc = TenantService(
            table_name=settings.TENANTS_TABLE_NAME,
            region=settings.AWS_REGION,
            secret_prefix=settings.DATABRICKS_TENANT_SECRET_PREFIX,
        )
    return _svc


def _tenant_projection(tenant: Tenant) -> TenantView:
    """The tenant plus its derived per-verb ``readiness``, computed on the way out so it cannot
    disagree with the stored ``capabilities`` and ``binding_mode``."""
    from services.platform_registry import tenant_readiness

    return TenantView(**tenant.model_dump(), readiness=tenant_readiness(tenant))


def _list_agents() -> list:
    from api.routes.agents import get_service

    return get_service().list()


def _list_mcp_servers() -> list:
    from api.routes.mcp_servers import get_service

    return get_service().list()


def _list_projects() -> list:
    from api.routes.projects import get_project_service

    return get_project_service().list_projects()


def _catalog_for(tenant: Tenant) -> RuntimeCatalog:
    """The discovery adapter for ``tenant``'s platform, built per request: an adapter holds a
    tenant-scoped credential path, so a shared instance would serve one tenant's client to
    another's request."""
    if tenant.platform == TenantPlatform.DATABRICKS:
        return DatabricksCatalog(region=settings.AWS_REGION)
    return AgentCoreCatalog(region=settings.AWS_REGION)


def _registered_handles() -> set:
    """Every runtime handle AGP already governs, as ``runtime_handle`` (Databricks) and
    ``agent_arn`` (AgentCore). Fails open: an unreadable registry drops the
    ``already_registered`` badge, not the listing, and registration re-checks anyway."""
    handles = set()
    try:
        records = _list_agents()
    except Exception:
        logger.exception("Could not read the agent registry for already_registered flags")
        return handles
    for record in records:
        for attr in ("runtime_handle", "agent_arn"):
            value = getattr(record, attr, None)
            if isinstance(value, str) and value:
                handles.add(value)
    return handles


def _is_referenced(tenant_id: str) -> bool:
    """True when any agent, MCP server or project names ``tenant_id``, or when a source cannot
    be read: an unverifiable source blocks the delete."""
    for source in (_list_agents, _list_mcp_servers, _list_projects):
        try:
            records = source()
        except Exception:
            logger.exception("Tenant reference check could not read a source; failing closed")
            return True
        if any(getattr(r, "tenant_id", None) == tenant_id for r in records):
            return True
    return False


@router.get("", response_model=list[TenantView])
async def list_tenants(
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.ADMIN)),
):
    return [_tenant_projection(t) for t in get_tenant_service().list()]


@router.post("", response_model=Tenant, status_code=201)
def create_tenant(
    body: TenantCreate,
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.ADMIN)),
):
    return get_tenant_service().create(body, created_by=principal.email or principal.oid or "")


@router.get("/{tenant_id}", response_model=Tenant)
async def get_tenant(
    tenant_id: str,
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.ADMIN)),
):
    return get_tenant_service().get(tenant_id)


@router.put("/{tenant_id}", response_model=Tenant)
def update_tenant(
    tenant_id: str,
    body: TenantUpdate,
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.ADMIN)),
):
    return get_tenant_service().update(tenant_id, body)


@router.post("/{tenant_id}/probe", response_model=TenantView)
def probe_tenant(
    tenant_id: str,
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.ADMIN)),
):
    """Re-run the connect-time probe against the stored config, persist, and return the tenant
    projection with ``readiness``. A POST: it re-connects rather than reads."""
    return _tenant_projection(get_tenant_service().reprobe(tenant_id))


@router.get("/{tenant_id}/discovered-agents", response_model=DiscoveredAgents)
async def list_discovered_agents(
    tenant_id: str,
    stage: str = Query(..., description="the tenant stage to discover on"),
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.ADMIN)),
):
    """The agents ``tenant_id`` runs on ``stage``, as its platform reports them, in a
    ``DiscoveredAgents`` envelope with the tenant's platform.

    404 for an unknown tenant; 400 for a stage the tenant does not list, decided before any
    platform call; 502 with a safe code for a discovery failure; 200 with an empty list when the
    platform was reached and reports no agents. ``stage`` is required: AGP names no stages, so
    there is no default to guess.
    """
    tenant = get_tenant_service().get(tenant_id)
    if stage not in tenant.stages:
        raise TenantError(f"tenant {tenant_id} lists no such stage", kind="unknown_stage")

    agents = await discover_agents(_catalog_for(tenant), tenant, stage)
    agents = mark_already_registered(agents, _registered_handles())
    return DiscoveredAgents(agents=agents, platform=tenant.platform)


@router.delete("/{tenant_id}", status_code=204)
async def delete_tenant(
    tenant_id: str,
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.ADMIN)),
):
    svc = get_tenant_service()
    svc.get(tenant_id)
    if _is_referenced(tenant_id):
        raise TenantError(f"tenant {tenant_id} is still referenced", kind="referenced")
    svc.delete(tenant_id)
    return Response(status_code=204)
