"""Repositories API: the flat ``/repositories`` list and the pull-request verbs on one repository.

A repository carries no tenant and no role of its own: both come through the parent project, so the
list filters on that project's tenant visibility and the caller's role on it, every pull-request verb
gates on them, and a repository whose project is not visible looks absent. Every pull-request verb runs
under the linked human's GitHub token. The project service and the gates resolve through
``api.routes.projects`` at call time.
"""

from typing import List, Optional

import anyio.to_thread
from fastapi import APIRouter
from fastapi import Depends as RBACDepends

import api.routes.projects as projects
from core.rbac import Principal, Role, current_principal, require_role
from models.github_link import GitHubUserLink, LinkStatus
from models.project import ProjectDetail
from models.project_role import ProjectRole
from models.repository import PullRequestCreate, PullRequestView, Repository
from services.github_pr_service import GitHubPrService, provider_coordinates
from services.github_user_link import GitHubLinkError
from services.project_errors import ProjectError
from services.project_resolver import ProjectContext
from services.tenant_resolver import TenantContext, visible

repositories_router = APIRouter(prefix="/repositories", tags=["repositories"])

_pr_svc: Optional[GitHubPrService] = None


@repositories_router.get("", response_model=List[Repository])
async def list_repositories(
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(projects.get_tenant_ctx),
    pctx: ProjectContext = RBACDepends(projects.get_project_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    """Every repository the caller may see. Repositories carry no tenant field and no role of their own:
    both are inherited through the parent project, and a repo whose project is not visible or gone is
    filtered out."""
    svc = projects.get_project_service()
    repos = await anyio.to_thread.run_sync(svc.list_repositories)
    if not ctx.is_global:
        project_tenant = {p.id: p.tenant_id for p in svc.list_projects()}
        repos = [
            r for r in repos if r.project_id in project_tenant and visible(ctx, project_tenant[r.project_id])
        ]
    if pctx.is_global:
        return repos
    rows = projects._governed_rows()
    pctx = projects._list_pctx(pctx, principal, rows)
    governed = projects._governed_project_ids(rows)
    return [
        r
        for r in repos
        if projects._may_project(pctx, r.project_id, ProjectRole.VIEWER, has_rows=r.project_id in governed)
    ]


def get_pr_service() -> GitHubPrService:
    """Lazy ``GitHubPrService`` singleton: it holds no credential and no store, only one pooled HTTP
    client. Tests patch this accessor."""
    global _pr_svc
    if _pr_svc is None:
        _pr_svc = GitHubPrService()
    return _pr_svc


def _load_visible_repo(repo_id: str, ctx: TenantContext) -> "tuple[Repository, ProjectDetail]":
    """Load a repository and its parent project, gated on tenant visibility through the parent. A missing
    repo, a repo whose project is gone and a repo in a foreign tenant raise the same 404."""
    svc = projects.get_project_service()
    repo = svc.get_repo(repo_id)
    if repo is None:
        raise ProjectError(f"no repository {repo_id}", kind="repo_not_found")
    detail = svc.get_project(repo.project_id)
    if detail is None or not visible(ctx, detail.project.tenant_id):
        raise ProjectError(f"repository {repo_id} has no visible project", kind="repo_not_found")
    return repo, detail


def _provider_coordinates(detail: ProjectDetail) -> "tuple[str, Optional[str]]":
    """The org and API base of the repo's connection, resolved from the project's connection record and
    never accepted from a caller, who acts under their own GitHub token here."""
    from api.routes.connections import get_connection_service

    return provider_coordinates(get_connection_service().get_connection, detail.project.connection_id)


def _linked_row(link_service, oid: str, connection_id: str) -> Optional[GitHubUserLink]:
    """The caller's live GitHub link for this connection, or None when they hold none or it was
    revoked. Scoped to the connection: a link to another org is not a link for this one."""
    for link in link_service.list_for_principal(oid):
        if link.connection_id == connection_id and link.status != LinkStatus.UNLINKED:
            return link
    return None


def _pr_context(
    repo_id: str,
    principal: Principal,
    ctx: TenantContext,
    pctx: ProjectContext,
    required: ProjectRole,
) -> "tuple[str, str, str, str, Optional[str]]":
    """Everything a pull-request verb needs, ``(org, repo_name, token, viewer_login, base_url)``,
    resolved in the one safe order: tenant visibility, project role, provider coordinates, then the
    linked human's token last, since resolving it can spend a refresh token.

    Every verb runs under the linked human's token and never AGP's own App token, which would let the
    platform approve a human's own pull request. A caller with no oid (dev auth), no link for this
    connection or a revoked one is refused with a 409 the UI can offer a remedy for, not a 401 (the SPA
    signs out on one) and not this router's 403 (their AGP standing is fine). The viewer login comes
    from the same link row, a provider currency, which decides ``can_approve``."""
    from api.routes.github_link import get_github_link_service

    repo, detail = _load_visible_repo(repo_id, ctx)
    projects._require_project_role_or_ungoverned(pctx, repo.project_id, required, principal=principal)
    org, base_url = _provider_coordinates(detail)
    connection_id = detail.project.connection_id
    oid = (principal.oid or "").strip()
    link_service = get_github_link_service()
    link = _linked_row(link_service, oid, connection_id) if oid else None
    if link is None:
        raise GitHubLinkError(f"no GitHub link on connection {connection_id}", kind="not_linked")
    token = link_service.get_user_bearer_token(oid, connection_id)
    return org, repo.name, token, link.github_login or "", base_url


@repositories_router.get("/{repo_id}/pull-requests", response_model=List[PullRequestView])
def list_pull_requests(
    repo_id: str,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(projects.get_tenant_ctx),
    pctx: ProjectContext = RBACDepends(projects.get_project_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    """This repository's pull requests as the linked human sees them; viewer-gated. The frontend calls
    it to decide whether the tab renders at all, so ``capability_missing`` is a mapped 403."""
    org, name, token, viewer, base_url = _pr_context(repo_id, principal, ctx, pctx, ProjectRole.VIEWER)
    return get_pr_service().list_pull_requests(org, name, token, viewer_login=viewer, base_url=base_url)


@repositories_router.post("/{repo_id}/pull-requests", response_model=PullRequestView, status_code=201)
def create_pull_request(
    repo_id: str,
    body: PullRequestCreate,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(projects.get_tenant_ctx),
    pctx: ProjectContext = RBACDepends(projects.get_project_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    """Open a pull request as the linked human; maintainer-gated, since it writes to the repository but
    deploys nothing. An omitted ``base`` means the repository's own default branch."""
    org, name, token, viewer, base_url = _pr_context(repo_id, principal, ctx, pctx, ProjectRole.MAINTAINER)
    return get_pr_service().create_pull_request(
        org,
        name,
        token,
        viewer_login=viewer,
        title=body.title,
        head=body.head,
        base=body.base,
        body=body.body,
        base_url=base_url,
    )


@repositories_router.post("/{repo_id}/pull-requests/{number}/approve", response_model=PullRequestView)
def approve_pull_request(
    repo_id: str,
    number: int,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(projects.get_tenant_ctx),
    pctx: ProjectContext = RBACDepends(projects.get_project_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    """Approve a pull request as the linked human, a repo-provider act and not the governance approval.
    The service refuses a self-approval and nothing is retried under another credential. A merge only
    registers a prod candidate, which still needs an owner's promote."""
    org, name, token, viewer, base_url = _pr_context(repo_id, principal, ctx, pctx, ProjectRole.MAINTAINER)
    return get_pr_service().approve_pull_request(
        org, name, number, token, viewer_login=viewer, base_url=base_url
    )


@repositories_router.post("/{repo_id}/pull-requests/{number}/merge", response_model=PullRequestView)
def merge_pull_request(
    repo_id: str,
    number: int,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(projects.get_tenant_ctx),
    pctx: ProjectContext = RBACDepends(projects.get_project_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    """Merge a pull request as the linked human; maintainer, since merging to the default branch
    registers a prod candidate and shipping it still needs an owner's promote."""
    org, name, token, viewer, base_url = _pr_context(repo_id, principal, ctx, pctx, ProjectRole.MAINTAINER)
    return get_pr_service().merge_pull_request(
        org, name, number, token, viewer_login=viewer, base_url=base_url
    )
