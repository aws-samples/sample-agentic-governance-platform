"""Repository lifecycle API under ``/projects/{id}/repos``: materialize, status, delivery history,
retry, build, promote, rollback, the delete preview and the delete.

Every route loads the parent project first, so a foreign tenant's project looks absent, then gates on
the caller's role on that project; repositories inherit the parent project's tenant and roles. The
project service and the gates resolve through ``api.routes.projects`` at call time.
"""

import functools
from typing import List, Optional

import anyio.to_thread
from fastapi import APIRouter, BackgroundTasks, Body, Query
from fastapi import Depends as RBACDepends

import api.routes.projects as projects
from core.rbac import Principal, Role, current_principal, require_role
from models.deployment import Deployment
from models.project_role import ProjectRole
from models.repository import (
    RepoDeletePreview,
    RepoDeleteResult,
    RepoDeleteSelection,
    RepoPromoteRequest,
    RepoRollbackRequest,
    Repository,
    RepositoryCreate,
)
from services.project_errors import ProjectError
from services.project_resolver import ProjectContext
from services.project_service import ProjectService
from services.tenant_resolver import TenantContext

router = APIRouter(prefix="/projects", tags=["projects"])


@router.post("/{id}/repos", response_model=Repository, status_code=202)
def add_repo(
    id: str,
    body: RepositoryCreate,
    background_tasks: BackgroundTasks,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(projects.get_tenant_ctx),
    pctx: ProjectContext = RBACDepends(projects.get_project_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    """Materialize a repository and its agent, a maintainer act: the service does the synchronous work
    (validate, pre-register, persist a pending record) and the materialize steps run as a background
    task after the 202. A sync handler, so the background task runs in the threadpool where the
    service's ``asyncio.run`` is safe."""
    projects._load_visible_project(id, ctx)
    projects._require_project_role_or_ungoverned(pctx, id, ProjectRole.MAINTAINER, principal=principal)
    svc = projects.get_project_service()
    repo = svc.add_repo(
        project_id=id,
        name=body.name,
        template_name=body.template_name,
        agent_config=body.agent_config.model_dump(exclude_unset=True),
        purpose=body.purpose,
        business_unit=body.business_unit,
        region=body.region,
        data_classification=body.data_classification,
        repo_overrides=body.repo_overrides,
        created_by=principal.email or principal.oid or "",
        principal=principal,
        mcp_name=body.mcp_name,
    )
    background_tasks.add_task(svc.run_materialize, repo.id)
    return repo


def _load_project_repo(svc: ProjectService, project_id: str, repo_id: str) -> Repository:
    repo = svc.get_repo(repo_id)
    if repo is None or repo.project_id != project_id:
        raise ProjectError(f"repository {repo_id} is not in project {project_id}", kind="repo_not_found")
    return repo


@router.get("/{id}/repos/{repo_id}/status", response_model=Repository)
async def repo_status(
    id: str,
    repo_id: str,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(projects.get_tenant_ctx),
    pctx: ProjectContext = RBACDepends(projects.get_project_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    """The live per-step materialize timeline; ``get_repo`` may reconcile a built gateway, so it runs
    off the event loop."""
    projects._load_visible_project(id, ctx)
    projects._require_project_role_or_ungoverned(pctx, id, ProjectRole.VIEWER, principal=principal)
    svc = projects.get_project_service()
    return await anyio.to_thread.run_sync(_load_project_repo, svc, id, repo_id)


@router.get("/{id}/repos/{repo_id}/deployments", response_model=List[Deployment])
async def repo_deployments(
    id: str,
    repo_id: str,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(projects.get_tenant_ctx),
    pctx: ProjectContext = RBACDepends(projects.get_project_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
    stage: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
):
    """One repo's delivery history, newest first, behind the same gate as ``/status``."""
    projects._load_visible_project(id, ctx)
    projects._require_project_role_or_ungoverned(pctx, id, ProjectRole.VIEWER, principal=principal)
    svc = projects.get_project_service()
    repo = await anyio.to_thread.run_sync(_load_project_repo, svc, id, repo_id)
    return await anyio.to_thread.run_sync(
        functools.partial(svc.list_deployments, repo.id, stage=stage, limit=limit)
    )


@router.post("/{id}/repos/{repo_id}/retry", response_model=Repository, status_code=202)
def retry_repo(
    id: str,
    repo_id: str,
    background_tasks: BackgroundTasks,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(projects.get_tenant_ctx),
    pctx: ProjectContext = RBACDepends(projects.get_project_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    """Resume a failed materialize from its first failed step, a maintainer act like ``add_repo``."""
    projects._load_visible_project(id, ctx)
    projects._require_project_role_or_ungoverned(pctx, id, ProjectRole.MAINTAINER, principal=principal)
    svc = projects.get_project_service()
    _load_project_repo(svc, id, repo_id)
    repo = svc.retry_materialize(repo_id)
    background_tasks.add_task(svc.run_materialize, repo_id)
    return repo


@router.post("/{id}/repos/{repo_id}/promote", response_model=Repository, status_code=202)
def promote_repo(
    id: str,
    repo_id: str,
    body: Optional[RepoPromoteRequest] = None,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(projects.get_tenant_ctx),
    pctx: ProjectContext = RBACDepends(projects.get_project_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    """Promote the repo's prod candidate to prod: the image registered when a merge to ``main`` landed,
    resolved server-side, never a tag from the caller. The only body field read is the optional
    ``override_reason`` for the eval gate. Owner through the strict gate, since a project with nobody
    accountable must not ship to production. A sync handler: the service does blocking boto3 work."""
    projects._load_visible_project(id, ctx)
    projects._require_project_role(pctx, id, ProjectRole.OWNER, principal=principal)
    return projects.get_project_service().promote_repo(
        id,
        repo_id,
        promoted_by=principal.oid or principal.email or "",
        override_reason=body.override_reason if body else None,
        promoted_by_name=principal.raw_claims.get("name") or principal.email,
    )


@router.post("/{id}/repos/{repo_id}/rollback", response_model=Repository, status_code=202)
def rollback_repo(
    id: str,
    repo_id: str,
    body: RepoRollbackRequest,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(projects.get_tenant_ctx),
    pctx: ProjectContext = RBACDepends(projects.get_project_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    """Redeploy a previously succeeded image tag. The tag is accepted only when it has a succeeded
    deployment row for this repo in this stage, which the service checks; the same strict owner gate as
    promote, since a write to production anywhere looser would be the real gate."""
    projects._load_visible_project(id, ctx)
    projects._require_project_role(pctx, id, ProjectRole.OWNER, principal=principal)
    return projects.get_project_service().rollback_repo(
        id,
        repo_id,
        image_tag=body.image_tag,
        stage=body.stage,
        rolled_back_by=principal.oid or principal.email or "",
    )


@router.post("/{id}/repos/{repo_id}/build", response_model=Repository, status_code=202)
def build_repo(
    id: str,
    repo_id: str,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(projects.get_tenant_ctx),
    pctx: ProjectContext = RBACDepends(projects.get_project_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    """Start a dev build of a config repo at its default branch's head, a maintainer act like ``retry``;
    prod stays behind promote's gate."""
    projects._load_visible_project(id, ctx)
    projects._require_project_role_or_ungoverned(pctx, id, ProjectRole.MAINTAINER, principal=principal)
    return projects.get_project_service().build_repo(
        id, repo_id, requested_by=principal.oid or principal.email or ""
    )


@router.get("/{id}/repos/{repo_id}/delete-preview", response_model=RepoDeletePreview)
async def delete_preview(
    id: str,
    repo_id: str,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(projects.get_tenant_ctx),
    pctx: ProjectContext = RBACDepends(projects.get_project_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    """The read-only reachability probe behind the delete modal, deleting nothing; owner-gated like the
    delete it precedes. Off the event loop: the runtime probe costs a tenant read and an STS round trip
    per stage."""
    projects._load_visible_project(id, ctx)
    projects._require_project_role_or_ungoverned(pctx, id, ProjectRole.OWNER, principal=principal)
    svc = projects.get_project_service()
    return await anyio.to_thread.run_sync(
        functools.partial(svc.preview_delete, project_id=id, repo_id=repo_id)
    )


@router.delete("/{id}/repos/{repo_id}", response_model=RepoDeleteResult)
def delete_repo(
    id: str,
    repo_id: str,
    selection: RepoDeleteSelection = Body(default_factory=RepoDeleteSelection),
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(projects.get_tenant_ctx),
    pctx: ProjectContext = RBACDepends(projects.get_project_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    """The teardown cascade over the selected steps, an owner act that never rides the ungoverned
    rule. Partial failure is data in the result envelope, not an HTTP error. The selection is the DELETE
    body, every step on by default. A sync handler: the service drives ``asyncio.run`` inside."""
    projects._load_visible_project(id, ctx)
    projects._require_project_role_or_ungoverned(pctx, id, ProjectRole.OWNER, principal=principal)
    return projects.get_project_service().delete_repo(project_id=id, repo_id=repo_id, selection=selection)
