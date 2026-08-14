"""User and authentication API routes"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from core.rbac import Principal, Role, _extract_role, current_principal, principal_email
from services.project_resolver import ProjectContext
from services.tenant_resolver import TenantContext

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/users", tags=["users"])

# Lazy TenantResolver singleton (E24/T4) — tests patch ``_tenant_resolver`` directly
# (the ``tenants.py`` ``_svc`` idiom), so this never runs against live AWS/Graph.
_tenant_resolver = None


def get_tenant_resolver():
    """Build the resolver from the app-wide singletons: the tenant service
    (``tenants.get_tenant_service``) + the ONE GraphService (``grants.get_graph_service``).
    Imported lazily/at call time to avoid import cycles (mirrors ``tenants._list_agents``).

    THE ONE resolver-singleton accessor in the app (E24/T5) — ``agents.py`` and
    ``mcp_servers.py`` import ``get_tenant_ctx`` below (which calls this) rather than
    keeping their own ``_tenant_resolver`` copies, so a test patching
    ``users_module._tenant_resolver`` observes every scoped route.
    """
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
    """Shared per-request tenant-scope dependency (E24/T5).

    Used by ``agents.py`` and ``mcp_servers.py`` (``RBACDepends(get_tenant_ctx)``) so
    every tenant-scoped route resolves the caller EXACTLY once per request via the
    SAME resolver singleton /users/me uses. Unlike ``users._resolve_tenants`` (which
    degrades to ``tenants=[]`` so a resolver failure never breaks the identity route),
    a failure here propagates — a scoped route must never silently treat a broken
    resolve as "caller sees nothing", which would mask an outage as an empty list
    instead of a 5xx.
    """
    return await get_tenant_resolver().resolve(principal)


# Lazy ProjectResolver singleton (E27/T3) — the project-role twin of ``_tenant_resolver``
# above. Tests patch ``_project_resolver`` directly, so this never runs against live
# AWS/Graph.
_project_resolver = None


def get_project_resolver():
    """Build the resolver from the app-wide singletons: the project-role service
    (``projects.get_project_role_service``) + the ONE GraphService
    (``grants.get_graph_service``). Imported lazily/at call time to avoid import cycles
    (mirrors ``get_tenant_resolver`` above).

    THE ONE project-resolver-singleton accessor in the app (E27/T3) — every
    project-scoped route reaches it through ``get_project_ctx`` below rather than
    keeping its own copy, so a test patching ``users_module._project_resolver``
    observes every gated route, and a role write in one route invalidates the SAME
    cache the next request reads.
    """
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
    """Shared per-request project-role dependency (E27/T3).

    Used by every project-scoped route (``RBACDepends(get_project_ctx)``) so the caller's
    per-project authority is resolved EXACTLY once per request via the SAME resolver
    singleton. Like ``get_tenant_ctx`` (and unlike ``_resolve_tenants``), a failure
    PROPAGATES — a gated route must never silently treat a broken resolve as "caller
    holds no role", which would mask an outage as a wall of 403s instead of a 5xx.
    """
    return await get_project_resolver().resolve(principal)


class UserInfo(BaseModel):
    email: str
    role: str
    role_level: int
    can_deploy: bool
    oid: Optional[str] = None
    # Entra tokens carry a human `name` claim (e.g. "Maria Bauer"); the FE shows
    # it in the sidebar footer and falls back to the email alias when None.
    name: Optional[str] = None
    # The caller's resolved tenant memberships (E24/T4). Admins get their
    # memberships too (possibly []) — `role` already tells the FE they're global.
    # A failed resolve degrades to [] (never breaks /users/me).
    #
    # Deliberately `list[dict]` and not a model: :func:`_resolve_tenants` is the single writer and
    # its projection is per-platform (E29/T9), so the shape is documented there — one place — rather
    # than split between a model that would have to make every platform's fields optional and a
    # function that decides which are actually sent. Each entry carries
    # ``{id, name, line_of_business, platform, binding_mode, stages}``.
    tenants: list[dict] = Field(default_factory=list)


def _project_stage(stage) -> dict:
    """Project ONE tenant stage for /users/me (E29/T9, OB-9).

    AWS stages are ``model_dump()``, byte-identical to pre-E29 — that branch is the fence.

    A **Databricks** stage is projected FIELD BY FIELD, and the omissions are the point:

    * ``account_id`` is kept as a KEY but always empty. On a Databricks stage that field holds the
      Databricks *account UUID*, and the Ops surfaces render ``stages[x].account_id`` under an
      "account" heading (``ProjectDetail.tsx`` prints ``{stage} account``). Sending the UUID would
      make that panel print a plausible-looking WRONG answer — worse than a crash, because nothing
      signals it. An empty string is not a claim, so the AWS-shaped reader shows nothing instead of
      something false. The key survives so no reader has to learn a second shape (this is a
      projection fix, not a redesign of a surface E29 does not own).
    * ``cloud``, ``sp_client_id`` and ``sp_client_secret_arn`` are dropped OUTRIGHT. No /users/me
      reader wants them, and a Secrets Manager pointer has no business being handed to every member
      of the tenant — the admin tenant route is where credential metadata belongs.

    What remains is what is TRUE and useful about a Databricks stage: which workspace it is
    (``workspace_url``, ``workspace_id``) and where (``region``).

    ``DatabricksStageConfig.sp_client_secret`` cannot leak here regardless — it is
    ``Field(exclude=True)``, so it is absent from every ``model_dump``. This function does not rely
    on that; it enumerates rather than filters, so a field added to the model in future is omitted
    by DEFAULT rather than published by default. That direction is deliberate.
    """
    from models.tenant import DatabricksStageConfig

    if isinstance(stage, DatabricksStageConfig):
        return {
            "workspace_url": stage.workspace_url,
            "workspace_id": stage.workspace_id,
            "region": stage.region,
            # AWS-shaped key, deliberately empty — see the docstring.
            "account_id": "",
        }
    return stage.model_dump()


async def _resolve_tenants(principal: Principal) -> list[dict]:
    """Resolve the caller's tenants via TenantResolver, degrading to [] on ANY
    failure — a broken Graph/DDB read must never take down /users/me.

    E29/T9 (OB-14) widens the projection with ``platform`` and ``binding_mode``. Their absence was
    a real defect, not an omission of convenience: the agent-registration wizard infers a tenant's
    platform from this projection and defaults an absent one to ``"aws"`` (the backend's own
    ``hydrate_tenant_item`` rule), so a NON-ADMIN operator on a Databricks tenant was handed the
    AgentCore branch — an ARN field for a platform that has no ARNs. The admin path was already
    correct because the admin tenant directory carries ``platform``; only non-admins, who cannot
    read that directory, were affected.

    Both are plain scalars off the record and neither is a credential. ``binding_mode`` rides along
    because the agent-detail badge reports it, and because a caller who can see the tenant can
    already see the agents whose invoke path it describes. It is REPORTING only — the invoke path
    re-reads the mode from the tenant record itself and never trusts a client's copy.
    """
    try:
        ctx = await get_tenant_resolver().resolve(principal)
    except Exception:  # noqa: BLE001 — degrade, never 500 the identity route.
        logger.exception("Tenant resolution failed for /users/me; returning tenants=[]")
        return []
    return [
        {
            "id": t.id,
            "name": t.name,
            "line_of_business": t.line_of_business,
            # ``str(...)`` because ``TenantPlatform`` is a ``str`` Enum: it serializes to its wire
            # value through FastAPI, but ``UserInfo.tenants`` is ``list[dict]`` (unmodelled), so
            # nothing downstream would coerce it and the raw member could reach the JSON encoder.
            # Explicit beats relying on a serializer that is not in this path.
            "platform": str(t.platform.value if hasattr(t.platform, "value") else t.platform),
            "binding_mode": t.binding_mode,
            "stages": {k: _project_stage(v) for k, v in t.stages.items()},
        }
        for t in ctx.tenants
    ]


@router.get("/me", response_model=UserInfo)
async def get_current_user(request: Request):
    """
    Return the current user's identity + RBAC role + resolved tenants.

    Precedence (mirrors core.rbac):
      - dev-auth (USE_DEV_AUTH/DEBUG) → identity from x-user-email header (or the
        default dev user); no token required.
      - else (Entra) → email from `rbac.principal_email` — THE single precedence
        `preferred_username` → `email` → `upn` — then "unknown" if the token
        carries none of the three.
    """
    # Settings() fresh inside the function so monkeypatched env vars in tests
    # are honored. See per-epic plan §"Convention discovered in T2".
    from core.config import Settings as _Settings
    settings_fresh = _Settings()

    role = _extract_role(request)

    auth = request.headers.get("Authorization", "")

    # current_principal reuses the SAME dev-auth/Entra precedence and the same
    # already-validated token, so the E24 tenant resolve sees the validated
    # identity (oid + raw_claims `groups`) in both branches.
    principal = current_principal(request)
    tenants = await _resolve_tenants(principal)

    # === Dev-auth path (local development only) ===
    if settings_fresh.USE_DEV_AUTH or settings_fresh.DEBUG:
        email = request.headers.get("x-user-email", "admin@example.com")
        return UserInfo(
            email=email,
            role=role.name.lower(),
            role_level=int(role),
            can_deploy=role >= Role.OPERATOR,
            # No Entra `oid` / human-name claim in dev-auth — the FE falls back
            # to the email alias.
            oid=None,
            name=None,
            tenants=tenants,
        )

    # === Entra path ===
    # _extract_role has already validated the token, so this decode is for
    # claim extraction only.
    email = "unknown"
    oid: Optional[str] = None
    name: Optional[str] = None
    if auth and auth.startswith("Bearer "):
        from core.security_entra import verify_entra_token

        try:
            claims = verify_entra_token(auth.split(" ", 1)[1])
            # ONE email precedence, shared with `current_principal` via
            # `rbac.principal_email` (E36/T18) — so the address shown here is
            # the address attributed to this caller's writes. The `"unknown"`
            # display fallback stays OUTSIDE the helper: it is a presentation
            # concern and must never reach a persisted `created_by`.
            email = principal_email(claims) or "unknown"
            # Entra object id — the stable principal identifier the
            # frontend compares against app-role-assignment principals
            # (E6 user→agent grants).
            oid = claims.get("oid")
            # Human display name (e.g. "Maria Bauer"). Optional claim — the
            # FE falls back to the email alias when it's absent.
            name = claims.get("name")
        except Exception:
            # _extract_role already passed; this re-decode shouldn't fail,
            # but if it does we don't want to 500 the route.
            email = "unknown"
    return UserInfo(
        email=email,
        role=role.name.lower(),
        role_level=int(role),
        can_deploy=role >= Role.OPERATOR,
        oid=oid,
        name=name,
        tenants=tenants,
    )
