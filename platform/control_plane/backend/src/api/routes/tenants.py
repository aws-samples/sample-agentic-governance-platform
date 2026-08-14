"""Tenant admin API (Epic 24 — multi-tenancy unit of ownership).

Admin-gated CRUD over Tenants. Structural clone of ``connections.py``: the lazy
``_svc`` / ``get_tenant_service()`` singleton (tests patch ``_svc`` directly so this
never runs against live AWS), and the FIXED-``detail`` convention in
``_raise_tenant_error`` — NEVER ``str(err)``, so a raw DDB ``ClientError`` can never
leak its message to the client.

RBAC: a Tenant maps line-of-business ownership onto Entra groups + AWS accounts —
a platform-admin surface. Unlike connections (operators read the picker), EVERY tenant
endpoint (list included) is ``require_role(Role.ADMIN)``: members do not enumerate
tenants here — they get their tenants from ``/users/me`` (spec §2, Task 4). ``created_by``
is taken from the validated ``principal.email``, NEVER from the body.

DELETE runs a reference check IN THE ROUTE (the service ``delete`` does none): a tenant
may only be removed once nothing points at it. We count ``tenant_id`` matches across the
agent registry, the MCP registry, and the project store (each read is guarded — ANY error
reading a source is treated as "cannot verify" and fails closed to 409, never a 500 or a
silent delete). ``tenant_id`` is read defensively (``getattr(r, "tenant_id", None)``)
because the agent/MCP models do not carry it until Task 4 — this route stands alone and
Task 4 lights the check up.

E29: tenants are platform-typed. ``platform`` is accepted on POST and rejected as unknown
by the model itself (422 before the service is called); PUT has no ``platform`` field at
all, so the key is dropped rather than obeyed. Per-platform stage rules live in the
service, and their failures arrive here as ``kind="validation"`` — so the FIXED-``detail``
mapping below already covers them: a rejected Databricks ``workspace_url`` returns
"invalid tenant" and the offending value never echoes back.

E29/T3 — DISCOVERY (``GET /{id}/discovered-agents``) and why POST/PUT ARE SYNC
-----------------------------------------------------------------------------
The discovery route answers "what agents does this tenant actually run?" for BOTH platforms
by dispatching on ``tenant.platform`` to a ``runtime_catalog`` adapter. The route owns four
things and nothing else: the admin gate, the stage guard (400 — a caller's mistake, kept
distinct from a 502 platform failure), the ``already_registered`` join against AGP's own
registry, and turning ANY discovery failure into a 502 with a SAFE CODE.

``create_tenant``/``update_tenant`` became **sync** handlers (FastAPI dispatches those to a
threadpool) because ``TenantService`` now drives the async capability probe via
``asyncio.run``, which raises if a loop is already running. This is exactly
``projects.add_repo``'s shape and it is load-bearing — as ``async def`` handlers, every
Databricks tenant write would 500 on a perfectly good credential.
"""

import logging
from dataclasses import asdict
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query, Response
from fastapi import Depends as RBACDepends

from core.config import settings
from core.rbac import Principal, Role, current_principal, require_role
from models.tenant import Tenant, TenantCreate, TenantPlatform, TenantUpdate
from services.runtime_catalog import (
    AgentCoreCatalog,
    CatalogError,
    DatabricksCatalog,
    RuntimeCatalog,
    mark_already_registered,
)
from services.tenant_service import TenantError, TenantService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/tenants", tags=["tenants"])

_svc: Optional[TenantService] = None

# TenantError.kind → HTTP status. FIXED detail literals keyed off the same hint — never
# str(err), which could carry a raw DDB message.
_ERROR_STATUS = {
    "not_found": 404,
    "name_taken": 409,
    "validation": 400,
    # E29/T3 (FIX round 1) — a Secrets Manager fault is 502, the ``connections.py`` idiom. NOT
    # 400 "invalid tenant": the operator's input was fine, so telling them it was invalid sends
    # them to edit a correct form instead of retrying. 5xx also says "ours, not yours", which is
    # what makes the same request worth repeating unchanged.
    "secret_error": 502,
}
_ERROR_DETAIL = {
    "not_found": "Tenant not found",
    "name_taken": "tenant name already exists",
    "validation": "invalid tenant",
    "secret_error": "Secret store operation failed",
}

_REFERENCED_DETAIL = "tenant is referenced by existing resources"

# E29/T3 — discovery. Fixed literals, same convention as above.
_UNKNOWN_STAGE_DETAIL = "unknown stage"
_DISCOVERY_FAILED_DETAIL = "platform discovery failed"


def get_tenant_service() -> TenantService:
    """Lazy ``TenantService`` singleton built from ``settings``.

    Empty ``TENANTS_TABLE_NAME`` ⇒ the service's in-memory fallback. Tests patch
    ``_svc`` directly with a fake service so this never runs against live AWS.
    """
    global _svc
    if _svc is None:
        _svc = TenantService(
            table_name=settings.TENANTS_TABLE_NAME,
            region=settings.AWS_REGION,
            # E29/T3 (FIX round 1): the Databricks credential prefix comes from settings, not
            # the service's module default — otherwise every environment writes tenant secrets
            # under the `agp-dev/` name regardless of stage.
            secret_prefix=settings.DATABRICKS_TENANT_SECRET_PREFIX,
        )
    return _svc


def _raise_tenant_error(err: TenantError) -> None:
    """Map a TenantError to an HTTPException with a FIXED detail literal (never str(err))."""
    status = _ERROR_STATUS.get(err.kind, 400)
    detail = _ERROR_DETAIL.get(err.kind, "invalid tenant")
    raise HTTPException(status_code=status, detail=detail)


# --- reference-check sources (module-level so tests patch them directly) ------
# Each returns the live records for one tenant-owning surface. Lazy imports keep this
# module import-safe and let the DELETE check reuse the app-wide service singletons.

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
    """Pick the discovery adapter for ``tenant``'s platform.

    **Platform dispatch lives HERE, not inside the seam.** The adapters know nothing about each
    other, and a factory that lived in ``runtime_catalog`` would make the module that defines
    the Protocol also depend on every implementation of it. Module-level so tests patch it
    directly (the ``_list_agents`` idiom above) and no live AWS/Databricks call is ever made.

    Built per request rather than cached: an adapter holds a tenant-scoped credential path, and
    a shared instance is how one tenant's client ends up serving another's request."""
    if tenant.platform == TenantPlatform.DATABRICKS:
        return DatabricksCatalog(region=settings.AWS_REGION)
    return AgentCoreCatalog(region=settings.AWS_REGION)


def _registered_handles() -> set:
    """Every runtime handle AGP already governs, in BOTH currencies.

    ``runtime_handle`` (Databricks) and ``agent_arn`` (AgentCore) are read from the same
    registry because one registry holds both platforms — matching only one would offer an
    operator a duplicate registration of every agent on the other.

    Fails OPEN, unlike the DELETE reference check above, and the asymmetry is deliberate: there
    the unverifiable answer permits a destructive act, so it must block; here it only removes a
    convenience flag from a read-only listing. Failing the whole discovery closed would hide
    every real agent because a badge could not be computed — and registration itself re-checks,
    so a duplicate is refused downstream regardless."""
    handles = set()
    try:
        records = _list_agents()
    except Exception:  # noqa: BLE001 — see the docstring (fail open, listing still ships).
        logger.exception("Could not read the agent registry for already_registered flags")
        return handles
    for record in records:
        for attr in ("runtime_handle", "agent_arn"):
            value = getattr(record, attr, None)
            if isinstance(value, str) and value:
                handles.add(value)
    return handles


def _is_referenced(tenant_id: str) -> bool:
    """True if any agent/MCP/project references ``tenant_id`` — OR if a source can't be
    read (fail-closed: an unverifiable source must block the delete, not permit it)."""
    for source in (_list_agents, _list_mcp_servers, _list_projects):
        try:
            records = source()
        except Exception:  # noqa: BLE001 — cannot verify ⇒ treat as referenced (fail closed).
            logger.exception("Tenant reference check could not read a source; failing closed")
            return True
        if any(getattr(r, "tenant_id", None) == tenant_id for r in records):
            return True
    return False


# --- routes ------------------------------------------------------------------

@router.get("", response_model=List[Tenant])
async def list_tenants(
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.ADMIN)),
):
    return get_tenant_service().list()


@router.post("", response_model=Tenant, status_code=201)
def create_tenant(
    body: TenantCreate,
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.ADMIN)),
):
    # A SYNC handler, deliberately (E29/T3): ``create`` drives the async Databricks capability
    # probe via ``asyncio.run``, which RAISES if a loop is already running. FastAPI dispatches
    # sync handlers to a threadpool, where there is none — the ``projects.add_repo`` shape. As
    # an ``async def`` this would 500 on every Databricks tenant with a working credential.
    svc = get_tenant_service()
    try:
        return svc.create(body, created_by=principal.email)
    except TenantError as err:
        _raise_tenant_error(err)


@router.get("/{tenant_id}", response_model=Tenant)
async def get_tenant(
    tenant_id: str,
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.ADMIN)),
):
    svc = get_tenant_service()
    try:
        return svc.get(tenant_id)
    except TenantError as err:
        _raise_tenant_error(err)


@router.put("/{tenant_id}", response_model=Tenant)
def update_tenant(
    tenant_id: str,
    body: TenantUpdate,
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.ADMIN)),
):
    # SYNC for the same reason as ``create_tenant`` — see the note there.
    svc = get_tenant_service()
    try:
        return svc.update(tenant_id, body)
    except TenantError as err:
        _raise_tenant_error(err)


@router.get("/{tenant_id}/discovered-agents")
async def list_discovered_agents(
    tenant_id: str,
    stage: str = Query(..., description="the tenant stage to discover on"),
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.ADMIN)),
):
    """The agents ``tenant_id`` actually runs on ``stage``, as its platform reports them.

    ``{"agents": [DiscoveredAgent...], "platform": "<tenant.platform>"}``. The registration
    wizard (T8) reads ``platform`` to decide which create body a selected row becomes, which is
    why the envelope carries it rather than leaving the client to re-derive it from the tenant.

    Status taxonomy, in order — each code answers a DIFFERENT operator question:

    * **404** the tenant does not exist (the existing mapping).
    * **400** the tenant has no such stage. The caller's mistake, and kept distinct from 502 so
      "I typed the wrong stage" never looks like "the platform is down".
    * **502** discovery failed, with a SAFE code in the detail. The upstream message can name
      workspace paths, IAM ARNs and principal ids, so only ``CatalogError.kind`` crosses —
      already shape-checked to ``^[A-Za-z_]{1,64}$`` on construction.
    * **200 with an empty list** the platform was reached and reports no agents. An ordinary
      answer (a Databricks SP with no app grants sees exactly this), and it MUST stay
      distinguishable from a failure or an operator goes hunting a credential that is fine.

    ``stage`` is REQUIRED (absent ⇒ 422): there is no sensible default. AGP names no stages
    (E28/D8 opened the axis), so guessing "dev" would silently discover the wrong workspace on
    any tenant that does not happen to have one.
    """
    svc = get_tenant_service()
    try:
        tenant = svc.get(tenant_id)
    except TenantError as err:
        _raise_tenant_error(err)

    # The stage guard runs BEFORE any platform call: an unknown stage must cost nothing.
    if stage not in tenant.stages:
        raise HTTPException(status_code=400, detail=_UNKNOWN_STAGE_DETAIL)

    try:
        agents = await _catalog_for(tenant).list_agents(tenant, stage)
    except CatalogError as err:
        raise HTTPException(
            status_code=502, detail=f"{_DISCOVERY_FAILED_DETAIL} ({err.kind})"
        ) from None
    except Exception:  # noqa: BLE001 — nothing from an adapter reaches a client raw.
        # An unexpected adapter exception is still a discovery failure, not a 500 whose detail
        # is shaped like a traceback. Logged with the traceback; answered with a fixed literal.
        logger.exception("Discovery failed for tenant %s", tenant_id)
        raise HTTPException(
            status_code=502, detail=f"{_DISCOVERY_FAILED_DETAIL} (unknown)"
        ) from None

    agents = mark_already_registered(agents, _registered_handles())
    return {"agents": [asdict(a) for a in agents], "platform": tenant.platform.value}


@router.delete("/{tenant_id}", status_code=204)
async def delete_tenant(
    tenant_id: str,
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.ADMIN)),
):
    svc = get_tenant_service()
    # Confirm the tenant exists first (404 before any reference work).
    try:
        svc.get(tenant_id)
    except TenantError as err:
        _raise_tenant_error(err)

    if _is_referenced(tenant_id):
        raise HTTPException(status_code=409, detail=_REFERENCED_DETAIL)

    try:
        svc.delete(tenant_id)
    except TenantError as err:
        _raise_tenant_error(err)
    return Response(status_code=204)
