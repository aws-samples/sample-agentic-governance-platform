"""The identity route ``GET /users/me`` and the two per-request scope dependencies every tenant- or
project-scoped route shares: ``get_tenant_ctx`` and ``get_project_ctx``.

Both resolvers are lazy module singletons built from the app-wide service singletons; tests assign
``_tenant_resolver`` / ``_project_resolver`` directly, so nothing here reaches AWS or Graph in the
suite.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Request
from pydantic import Field

from core.rbac import Principal, Role, current_principal
from models.base import WireModel
from models.user import UserTenant
from services.project_resolver import ProjectContext
from services.tenant_resolver import TenantContext

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/users", tags=["users"])

_tenant_resolver = None


def get_tenant_resolver():
    """The one ``TenantResolver`` accessor in the app, built on first use from the tenant service and
    the one ``GraphService``; the imports are at call time to avoid import cycles. ``agents.py`` and
    ``mcp_servers.py`` reach it through ``get_tenant_ctx`` rather than keeping their own copy, so a
    test that patches ``_tenant_resolver`` here observes every scoped route."""
    global _tenant_resolver
    if _tenant_resolver is None:
        from api.routes.grants import get_graph_service
        from api.routes.tenants import get_tenant_service
        from services.tenant_resolver import TenantResolver

        _tenant_resolver = TenantResolver(get_tenant_service(), get_graph_service())
    return _tenant_resolver


async def get_tenant_ctx(
    principal: Principal = Depends(current_principal),
) -> TenantContext:
    """Per-request tenant scope, resolved once per request through the same resolver ``/users/me``
    uses. Unlike ``_resolve_tenants`` a failure propagates: a scoped route must never read a broken
    resolve as "the caller sees nothing", which would mask an outage as an empty list."""
    return await get_tenant_resolver().resolve(principal)


_project_resolver = None


def get_project_resolver():
    """The one ``ProjectResolver`` accessor in the app, the project-role twin of
    ``get_tenant_resolver``: built on first use from the project-role service and the one
    ``GraphService``, reached by every project-scoped route through ``get_project_ctx``, so a role
    write in one route invalidates the same cache the next request reads."""
    global _project_resolver
    if _project_resolver is None:
        from api.routes.grants import get_graph_service
        from api.routes.projects import get_project_role_service
        from services.project_resolver import ProjectResolver

        _project_resolver = ProjectResolver(get_project_role_service(), get_graph_service())
    return _project_resolver


async def get_project_ctx(
    principal: Principal = Depends(current_principal),
) -> ProjectContext:
    """Per-request project-role scope, resolved once per request. Like ``get_tenant_ctx`` a failure
    propagates: a gated route must never read a broken resolve as "the caller holds no role", which
    would mask an outage as a wall of 403s."""
    return await get_project_resolver().resolve(principal)


class UserInfo(WireModel):
    email: str
    role: str
    role_level: int
    can_deploy: bool
    oid: Optional[str] = None
    name: Optional[str] = Field(
        default=None,
        description="The token's human `name` claim; the interface falls back to the email alias when null",
    )
    tenants: list[UserTenant] = Field(
        default_factory=list,
        description=(
            "The caller's resolved tenant memberships, admins included; [] when none or when the "
            "resolve failed. Each entry carries id, name, line_of_business, platform, binding_mode, "
            "readiness and stages, projected per platform by the route."
        ),
    )


def _project_stage(stage) -> dict:
    """One tenant stage as ``/users/me`` sends it.

    An AWS stage is ``model_dump()``. A Databricks stage is enumerated field by field: ``workspace_url``,
    ``workspace_id`` and ``region`` cross; ``account_id`` stays a key but is always empty, because on a
    Databricks stage it holds the Databricks account UUID and the AWS-shaped readers would print it
    under an "account" heading; ``cloud``, ``sp_client_id`` and ``sp_client_secret_arn`` are dropped,
    since no reader here wants them and credential metadata belongs to the admin tenant route.
    Enumerating rather than filtering means a field added to the model is omitted by default.
    """
    from models.tenant import DatabricksStageConfig

    if isinstance(stage, DatabricksStageConfig):
        return {
            "workspace_url": stage.workspace_url,
            "workspace_id": stage.workspace_id,
            "region": stage.region,
            "account_id": "",
        }
    return stage.model_dump()


async def _resolve_tenants(principal: Principal) -> list[UserTenant]:
    """The caller's tenants through the resolver, projected for the wire; ``[]`` on any failure, so
    a broken Graph or DynamoDB read never takes down ``/users/me``.

    ``platform`` and ``binding_mode`` are present on every entry (the registration wizard infers
    the platform from this projection and would guess "aws" for an absent key), ``readiness`` is
    derived per verb and never stored, and every value is reporting only: the invoke path re-reads
    the tenant record and never trusts a client's copy.
    """
    try:
        ctx = await get_tenant_resolver().resolve(principal)
    except Exception:
        logger.exception("Tenant resolution failed for /users/me; returning tenants=[]")
        return []
    from services.platform_registry import tenant_readiness

    return [
        UserTenant.model_validate(
            {
                "id": t.id,
                "name": t.name,
                "line_of_business": t.line_of_business,
                "platform": str(t.platform.value if hasattr(t.platform, "value") else t.platform),
                "binding_mode": t.binding_mode,
                "readiness": tenant_readiness(t),
                "stages": {k: _project_stage(v) for k, v in t.stages.items()},
            }
        )
        for t in ctx.tenants
    ]


@router.get("/me", response_model=UserInfo)
async def get_current_user(request: Request):
    """The caller's identity, RBAC role and resolved tenants.

    Under dev-auth (``Settings.dev_auth_honoured``) the email is the ``x-user-email`` header or the
    default dev user and no token is read. Under Entra the identity is the validated principal's:
    the email follows ``rbac.principal_email`` and is ``"unknown"`` when the token carries none of the
    three claims. That display fallback lives here and never in ``Principal``, so it cannot reach a
    persisted ``created_by``.
    """
    from core.config import Settings as _Settings

    settings_fresh = _Settings()

    principal = current_principal(request)
    role = principal.role
    tenants = await _resolve_tenants(principal)

    if settings_fresh.dev_auth_honoured:
        return UserInfo(
            email=request.headers.get("x-user-email", "admin@example.com"),
            role=role.name.lower(),
            role_level=int(role),
            can_deploy=role >= Role.OPERATOR,
            oid=None,
            name=None,
            tenants=tenants,
        )

    return UserInfo(
        email=principal.email or "unknown",
        role=role.name.lower(),
        role_level=int(role),
        can_deploy=role >= Role.OPERATOR,
        oid=principal.oid,
        name=principal.raw_claims.get("name"),
        tenants=tenants,
    )
