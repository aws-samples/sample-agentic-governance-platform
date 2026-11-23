"""Projects (containers) API, and the services and gates every project and repository route shares.

Operator-gated CRUD over projects and their per-project roles. A project is an empty container scoped
to one org connection and one tenant.

Every project route gates on tenant visibility first, so a foreign tenant's project looks absent (the
same 404 as a missing id), then on the caller's per-project role through ``may()``, the only authority
seam. A project with no role rows is ungoverned and keeps maintainer-level verbs open to tenant members,
never owner verbs, and a refusal is confirmed against a fresh store read before it is served, because
the resolver's cache is per process. Service errors render through ``core.errors.AppError``'s handler;
the refusals raised here are the route's own: visibility, project role, tenant membership on create.
``created_by`` and every attribution come from the validated principal, never the body.
"""

import logging
from typing import List, Optional

from fastapi import APIRouter, Response
from fastapi import Depends as RBACDepends

from api.routes.agents import (
    get_identity_service,
    get_langfuse_service,
)
from api.routes.agents import (
    get_service as get_agent_registry,
)
from core.config import settings
from core.rbac import Principal, Role, current_principal, require_role
from models.project import Project, ProjectCreate, ProjectDetail
from models.project_role import ProjectRole, ProjectRoleCreate, ProjectRoleRecord, role_name
from services.connection_service import ConnectionService
from services.ecr_image_service import EcrImageService
from services.project_errors import ProjectError
from services.project_resolver import ProjectContext, context_from_rows, may, widen
from services.project_role_service import ProjectRoleError, ProjectRoleService
from services.project_service import ProjectService
from services.template_registry import TemplateRegistry
from services.tenant_resolver import TenantContext, require_member, visible

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/projects", tags=["projects"])

_svc: Optional[ProjectService] = None
_role_svc: Optional[ProjectRoleService] = None


class ProjectRoleRead(ProjectRoleRecord):
    """Response model for the role routes: ``ProjectRoleRecord``'s fields verbatim. A role row carries
    only metadata, so the stored record is safe to return as is."""


def get_project_role_service() -> ProjectRoleService:
    """Lazy ``ProjectRoleService`` singleton: roles live in a third partition of the projects table.
    ``users.get_project_resolver()`` builds the one ``ProjectResolver`` from the same instance, so the
    role writes here and the authorization reads there share one store."""
    global _role_svc
    if _role_svc is None:
        _role_svc = ProjectRoleService(table_name=settings.PROJECTS_TABLE_NAME, region=settings.AWS_REGION)
    return _role_svc


async def get_tenant_ctx(
    principal: Principal = RBACDepends(current_principal),
) -> TenantContext:
    """The per-request tenant context through ``users.py``'s one resolver singleton, so tests that patch
    ``api.routes.users._tenant_resolver`` cover every route here. Imported lazily to break the import
    cycle between the two route modules."""
    from api.routes.users import get_tenant_ctx as _users_get_tenant_ctx

    return await _users_get_tenant_ctx(principal)


async def get_project_ctx(
    principal: Principal = RBACDepends(current_principal),
) -> ProjectContext:
    """The per-request project context through ``users.py``'s one resolver singleton; see
    ``get_tenant_ctx``."""
    from api.routes.users import get_project_ctx as _users_get_project_ctx

    return await _users_get_project_ctx(principal)


def _require_project_role(
    pctx: ProjectContext, project_id: str, required: ProjectRole, *, principal: Principal
) -> ProjectContext:
    """The strict gate, with no ungoverned-project rule: the role roster routes (the roster is the
    governance surface) and the two production verbs, promote and rollback. Runs after
    ``_load_visible_project``. A refusal is confirmed against a fresh read first, and the context the
    gate decided on is returned so a caller reporting the caller's standing cannot disagree with it."""
    if may(pctx, project_id, required):
        return pctx
    fresh = _refreshed_pctx(pctx, principal, project_id)
    if may(fresh, project_id, required):
        return fresh
    raise ProjectRoleError(f"{role_name(required)} needed on project {project_id}", kind="insufficient")


def _may_project(pctx: ProjectContext, project_id: str, required: ProjectRole, *, has_rows: bool) -> bool:
    """``may()`` with the ungoverned-project rule: a project with no role rows admits tenant-visible
    callers up to maintainer; owner verbs always need a real row."""
    if may(pctx, project_id, required):
        return True
    return not has_rows and required <= ProjectRole.MAINTAINER


def _require_project_role_or_ungoverned(
    pctx: ProjectContext, project_id: str, required: ProjectRole, *, principal: Principal
) -> ProjectContext:
    """The gate on every other project route: ``may()`` first, so the store read for the ungoverned rule
    happens only on the deny path, then the fresh-read confirmation, since ``has_role_rows`` is a live
    read that withholds the rule the moment the creator's owner row exists on another task."""
    if may(pctx, project_id, required):
        return pctx
    if _may_project(pctx, project_id, required, has_rows=_project_has_role_rows(project_id)):
        return pctx
    fresh = _refreshed_pctx(pctx, principal, project_id)
    if may(fresh, project_id, required):
        return fresh
    raise ProjectRoleError(f"{role_name(required)} needed on project {project_id}", kind="insufficient")


def _refreshed_pctx(pctx: ProjectContext, principal: Principal, project_id: str) -> ProjectContext:
    """Re-resolve one project from a fresh store read on the deny path, since another task's grant is
    invisible to this process's cache for up to a TTL. The result is only ever ORed onto a refusal, so it
    can restore authority the store says the caller holds and never widen anything. Fails closed: a
    store fault or a resolver failure hands the original context back and the refusal stands."""
    from api.routes.users import get_project_resolver

    try:
        return get_project_resolver().refresh_project(principal, project_id)
    except ProjectRoleError:
        logger.warning(
            "Could not re-read the role rows for project %s while confirming a refusal; the refusal stands",
            project_id,
        )
        return pctx
    except Exception:
        logger.exception("Failed to refresh project roles for %s while confirming a refusal", project_id)
        return pctx


def _project_has_role_rows(project_id: str) -> bool:
    """Whether the project is governed, through the strict read: an unreadable partition or a refused id
    means it cannot be established that the project is ungoverned, so the rule is withheld and only the
    caller's real role decides."""
    try:
        return get_project_role_service().has_role_rows(project_id)
    except ProjectRoleError:
        logger.warning(
            "Could not read the role rows for project %s; treating it as governed so the "
            "ungoverned-project rule cannot apply",
            project_id,
        )
        return True


def _governed_rows() -> List[ProjectRoleRecord]:
    """Every role row in the partition, the two list routes' single read for both the governed-project
    set and the caller's own fresh roles. The strict read: an unreadable partition surfaces as the
    store's ``ownership_unverified`` row instead of an empty governed set, which would leak every
    governed project in the tenant."""
    return get_project_role_service().list_all_strict()


def _list_pctx(pctx: ProjectContext, principal: Principal, rows: List[ProjectRoleRecord]) -> ProjectContext:
    """Fold the list routes' already-read rows into the caller's context, widening only, so the list and
    the detail page agree about a just-granted project without a second read."""
    return widen(pctx, context_from_rows(principal, rows))


def _governed_project_ids(rows: List[ProjectRoleRecord]) -> frozenset[str]:
    return frozenset(r.project_id for r in rows)


def _effective_role_hint(pctx: ProjectContext, project_id: str) -> Optional[str]:
    """The caller's effective role on ``project_id`` as a wire name, a UI hint and never an authority:
    the browser cannot evaluate an Entra group grant. A global admin reads as owner."""
    if pctx.is_global:
        return role_name(ProjectRole.OWNER)
    held = pctx.roles.get(project_id)
    return None if held is None else role_name(held)


def _load_visible_project(id: str, ctx: TenantContext) -> ProjectDetail:
    """Load a project and gate it on tenant visibility before any side effect; a missing and a foreign
    project raise the same 404."""
    detail = get_project_service().get_project(id)
    if detail is None or not visible(ctx, detail.project.tenant_id):
        raise ProjectError(f"project {id} is not visible", kind="not_found")
    return detail


def get_project_service() -> ProjectService:
    """Lazy ``ProjectService`` singleton built from ``settings``, sharing the agents route's registry,
    identity and Langfuse singletons and the MCP routes' registry and identity singletons. The runtime
    build service is attached after ``_svc`` is published because ``builds.py``'s factory resolves this
    one back; the cycle is broken by order."""
    global _svc
    if _svc is None:
        connection_service = ConnectionService(
            table_name=settings.CONNECTIONS_TABLE_NAME,
            secret_prefix=settings.CONNECTIONS_SECRET_PREFIX,
            region=settings.AWS_REGION,
        )
        repo_vars = {
            "AWS_REGION": settings.AWS_REGION,
            "AWS_ECR_PUSH_ROLE_ARN": settings.PROJECT_ECR_PUSH_ROLE_ARN,
            "ECR_REPOSITORY": settings.PROJECT_ECR_REPOSITORY,
            "PLATFORM_BUILD_DONE_URL": settings.PROJECT_BUILD_DONE_URL,
            "AGP_API_URL": settings.AGP_API_URL,
        }
        from api.routes.builds import get_build_service
        from api.routes.mcp_servers import get_mcp_identity_service
        from api.routes.mcp_servers import get_service as get_mcp_registry
        from services.tenant_service import TenantService

        tenant_service = TenantService(table_name=settings.TENANTS_TABLE_NAME, region=settings.AWS_REGION)
        _svc = ProjectService(
            table_name=settings.PROJECTS_TABLE_NAME,
            registry=get_agent_registry(),
            identity=get_identity_service(),
            connection_service=connection_service,
            tenant_service=tenant_service,
            repo_vars=repo_vars,
            region=settings.AWS_REGION,
            ecr_image_service=EcrImageService(
                repository=settings.PROJECT_ECR_REPOSITORY, region=settings.AWS_REGION
            ),
            runtime_build_service=None,
            ecr_repository=settings.PROJECT_ECR_REPOSITORY,
            langfuse_provisioning=get_langfuse_service(),
            runtime_state_bucket=settings.RUNTIME_MODULE_BUCKET,
            agent_templates_dir=settings.AGENT_TEMPLATES_DIR,
            template_registry=TemplateRegistry(
                table_name=settings.PROJECTS_TABLE_NAME, region=settings.AWS_REGION
            ),
            mcp_registry=get_mcp_registry(),
            mcp_identity=get_mcp_identity_service(),
        )
        _svc.attach_runtime_build_service(get_build_service())
    return _svc


@router.post("", response_model=Project, status_code=201)
async def create_project(
    body: ProjectCreate,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    """Create an empty container. The tenant must exist (the tenant service's ``not_found`` row) and,
    for a non-global caller, be one of their memberships: no resource exists yet, so that refusal is a
    403. The creator becomes the project's first owner."""
    from api.routes.tenants import get_tenant_service

    get_tenant_service().get(body.tenant_id)
    require_member(ctx, body.tenant_id)

    svc = get_project_service()
    project = svc.create_project(
        name=body.name,
        connection_id=body.connection_id,
        tenant_id=body.tenant_id,
        description=body.description,
        created_by=principal.email or principal.oid or "",
    )
    _grant_creator_owner(project.id, principal)
    return project


def _grant_creator_owner(project_id: str, principal: Principal) -> None:
    """Grant the creator owner on the new project, keyed on ``principal.oid`` because a role row's
    ``principal_id`` must be an Entra object id for the resolver to match it. A dev-auth principal has no
    oid and is skipped. A store failure is logged, never propagated: the project is already persisted and
    an ungoverned project is the safe outcome."""
    if not principal.oid:
        logger.info(
            "Skipping creator-OWNER grant for project %s: no Entra oid on the principal "
            "(dev-auth); the project starts with no role rows.",
            project_id,
        )
        return
    try:
        get_project_role_service().grant(
            project_id,
            ProjectRoleCreate(
                principal_id=principal.oid,
                principal_type="user",
                principal_display=principal.email or principal.oid,
                role=role_name(ProjectRole.OWNER),
            ),
            granted_by=principal.oid,
        )
    except Exception:
        logger.exception("Failed to grant creator-OWNER on project %s", project_id)
        return
    _invalidate_project_roles()


def _invalidate_project_roles() -> None:
    """Drop this process's cached role rows after a role write so the change applies on the next request
    instead of up to one TTL later. Best effort: a failure only means the change lands a minute late."""
    from api.routes.users import get_project_resolver

    try:
        get_project_resolver().invalidate()
    except Exception:
        logger.exception("Failed to invalidate the project-role cache")


@router.get("", response_model=List[Project])
async def list_projects(
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    pctx: ProjectContext = RBACDepends(get_project_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    """The caller's projects: the tenant post-filter, then the project-role filter from one
    whole-partition read (a global admin skips it)."""
    svc = get_project_service()
    projects = [p for p in svc.list_projects() if visible(ctx, p.tenant_id)]
    if pctx.is_global:
        return projects
    rows = _governed_rows()
    pctx = _list_pctx(pctx, principal, rows)
    governed = _governed_project_ids(rows)
    return [p for p in projects if _may_project(pctx, p.id, ProjectRole.VIEWER, has_rows=p.id in governed)]


@router.get("/{id}", response_model=ProjectDetail)
async def get_project(
    id: str,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    pctx: ProjectContext = RBACDepends(get_project_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    """The project with its repositories and the two UI hints, derived from the context the gate decided
    on. ``ungoverned`` costs no extra read: a non-global caller holding no role can only have passed
    through the ungoverned rule."""
    detail = _load_visible_project(id, ctx)
    pctx = _require_project_role_or_ungoverned(pctx, id, ProjectRole.VIEWER, principal=principal)
    return detail.model_copy(
        update={
            "effective_role": _effective_role_hint(pctx, id),
            "ungoverned": not pctx.is_global and pctx.roles.get(id) is None,
        }
    )


@router.get("/{id}/roles", response_model=List[ProjectRoleRead])
async def list_project_roles(
    id: str,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    pctx: ProjectContext = RBACDepends(get_project_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    """The project's role roster, viewer-gated, through the strict read: the console turns an empty
    roster into a decision, so "empty" and "unreadable" must be distinguishable on the wire."""
    _load_visible_project(id, ctx)
    _require_project_role(pctx, id, ProjectRole.VIEWER, principal=principal)
    return get_project_role_service().list_for_project_strict(id)


@router.post("/{id}/roles", response_model=ProjectRoleRead, status_code=201)
async def grant_project_role(
    id: str,
    body: ProjectRoleCreate,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    pctx: ProjectContext = RBACDepends(get_project_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    """Grant (or re-grant, the store upserts) one principal's role; owner-gated."""
    _load_visible_project(id, ctx)
    _require_project_role(pctx, id, ProjectRole.OWNER, principal=principal)
    record = get_project_role_service().grant(id, body, granted_by=principal.oid or principal.email or "")
    _invalidate_project_roles()
    return record


@router.put("/{id}/roles/{principal_id}", response_model=ProjectRoleRead)
async def update_project_role(
    id: str,
    principal_id: str,
    body: ProjectRoleCreate,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    pctx: ProjectContext = RBACDepends(get_project_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    """Change one principal's role: a re-grant keyed on the path's ``principal_id``, so a body naming
    another principal cannot retarget the write."""
    _load_visible_project(id, ctx)
    _require_project_role(pctx, id, ProjectRole.OWNER, principal=principal)
    record = get_project_role_service().grant(
        id,
        body.model_copy(update={"principal_id": principal_id}),
        granted_by=principal.oid or principal.email or "",
    )
    _invalidate_project_roles()
    return record


@router.delete("/{id}/roles/{principal_id}", status_code=204)
async def revoke_project_role(
    id: str,
    principal_id: str,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    pctx: ProjectContext = RBACDepends(get_project_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    _load_visible_project(id, ctx)
    _require_project_role(pctx, id, ProjectRole.OWNER, principal=principal)
    get_project_role_service().revoke(id, principal_id)
    _invalidate_project_roles()
    return Response(status_code=204)


@router.delete("/{id}", status_code=204)
async def delete_project(
    id: str,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    pctx: ProjectContext = RBACDepends(get_project_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    """Delete an empty container (the service refuses while repositories exist), an owner act, then its
    role rows."""
    _load_visible_project(id, ctx)
    _require_project_role_or_ungoverned(pctx, id, ProjectRole.OWNER, principal=principal)
    get_project_service().delete_project(id)
    _cleanup_project_roles(id)
    return Response(status_code=204)


def _cleanup_project_roles(project_id: str) -> None:
    """Delete the project's role rows after the project itself is gone, so a failure can only leave
    orphan rows, never a live project whose owners were stripped. Best effort: raising would 500 a delete
    that succeeded; ``revoke_all`` is idempotent, so a later delete of the same id cleans up."""
    try:
        removed = get_project_role_service().revoke_all(project_id)
    except Exception:
        logger.exception(
            "Failed to delete the role rows for project %s; the project is gone but its role rows "
            "remain orphaned",
            project_id,
        )
        return
    if removed:
        logger.info("Deleted %d role row(s) for project %s", removed, project_id)
    _invalidate_project_roles()
