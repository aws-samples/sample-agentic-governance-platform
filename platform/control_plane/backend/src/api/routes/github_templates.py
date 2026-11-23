"""Template catalog admin API: list, upload a zip, patch, deregister; plus the infra catalog's read.

The catalog is AGP's own ``template`` registry partition, so DELETE deregisters and leaves the
repository in place: a 204 means "no longer in the catalog", never "the repo is gone". Every
operation is scoped to a ``connection_id``; reads need OPERATOR and writes ADMIN.
``GitHubTemplateError`` and ``InfraTemplateRegistryError`` render through their tables.

``infra_templates_router`` is a second router in this module because its path is
``/projects/templates/infra`` while ``router`` is prefixed ``/github-templates``; it lives here,
with the agent-template catalog it is the other half of, because both read the same projects table
and the ``TemplateView`` join needs the same registry singleton.
"""

import logging
from typing import List, Optional

from fastapi import APIRouter, File, Form, Response, UploadFile
from fastapi import Depends as RBACDepends

from core.config import settings
from core.rbac import Principal, Role, current_principal, require_role
from models.base import WireModel
from services.connection_service import ConnectionService
from services.github_repo_service import GitHubRepoService
from services.github_template_service import GitHubTemplateService, TemplateView
from services.infra_template_registry import (
    InfraTemplateRecord,
    InfraTemplateRegistry,
    InfraTemplateRegistryError,
)
from services.template_registry import TemplateRegistry

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/github-templates", tags=["github-templates"])
infra_templates_router = APIRouter(prefix="/projects", tags=["github-templates"])

_svc: Optional[GitHubTemplateService] = None
_infra_registry: Optional[InfraTemplateRegistry] = None

_FALLBACK_PLATFORM = "aws"
_FALLBACK_BUILD_KIND = "container"


class InfraTemplateView(WireModel):
    """One infra-template catalog entry: a projection of :class:`InfraTemplateRecord` without
    ``module_source`` (an internal S3 key), ``connection_id`` (the caller's own query parameter)
    and the audit fields. The console renders the deploy mechanism, not a storage path."""

    id: str
    name: str
    description: str = ""
    platform: str
    iac_type: str
    build_kind: str
    repo_name: str
    version: str = "1"
    updated_at: str = ""


class TemplatePatch(WireModel):
    """Write-only PATCH body: editable metadata only, never the name."""

    description: Optional[str] = None
    aws_services: Optional[List[str]] = None
    tags: Optional[List[str]] = None
    framework: Optional[str] = None

    class Config:
        extra = "ignore"


def get_github_template_service() -> GitHubTemplateService:
    """Lazy ``GitHubTemplateService`` singleton: a real ``GitHubRepoService`` for the upload's repo
    create, a ``ConnectionService`` for the org, base URL and token, and the ``TemplateRegistry``
    over the ``template`` partition of the projects table. Tests patch ``_svc`` directly."""
    global _svc
    if _svc is None:
        _svc = GitHubTemplateService(
            github_repo_service=GitHubRepoService(),
            connection_service=ConnectionService(
                table_name=settings.CONNECTIONS_TABLE_NAME,
                secret_prefix=settings.CONNECTIONS_SECRET_PREFIX,
                region=settings.AWS_REGION,
            ),
            template_registry=TemplateRegistry(
                table_name=settings.PROJECTS_TABLE_NAME,
                region=settings.AWS_REGION,
            ),
        )
    return _svc


def get_infra_template_registry() -> InfraTemplateRegistry:
    """Lazy ``InfraTemplateRegistry`` singleton over another partition of the same projects
    table. Tests patch ``_infra_registry`` directly."""
    global _infra_registry
    if _infra_registry is None:
        _infra_registry = InfraTemplateRegistry(
            table_name=settings.PROJECTS_TABLE_NAME,
            region=settings.AWS_REGION,
        )
    return _infra_registry


@infra_templates_router.get("/templates/infra", response_model=List[InfraTemplateView])
async def list_infra_templates(
    connection_id: str,
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    """The connection's infra catalog: which deploy mechanisms exist. A store fault is a 503,
    never an empty list, since "no infra templates" would tell the console every agent template is
    incompatible with every tenant."""
    records = get_infra_template_registry().list(connection_id)
    return [_infra_view_from_record(r) for r in records]


@router.get("", response_model=List[TemplateView])
async def list_templates(
    connection_id: str,
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    views = get_github_template_service().list_templates(connection_id)
    return _joined_with_infra(connection_id, views)


@router.post("", response_model=TemplateView, status_code=201)
async def upload_template(
    connection_id: str = Form(...),
    name: str = Form(...),
    framework: str = Form(...),
    description: str = Form(""),
    aws_services: List[str] = Form(default=[]),
    tags: List[str] = Form(default=[]),
    file: UploadFile = File(...),
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.ADMIN)),
):
    """``created_by`` is the validated principal, never a form value; the answer is the joined
    view, like every other route that returns a ``TemplateView``."""
    svc = get_github_template_service()
    zip_bytes = await file.read()
    view = svc.upload_template(
        connection_id,
        zip_bytes=zip_bytes,
        name=name,
        description=description,
        framework=framework,
        aws_services=aws_services,
        tags=tags,
        created_by=principal.email or principal.oid or "",
    )
    return joined_template_view(connection_id, view)


@router.patch("/{name}", response_model=TemplateView)
async def patch_template(
    name: str,
    connection_id: str,
    body: TemplatePatch,
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.ADMIN)),
):
    view = get_github_template_service().patch_template(
        connection_id,
        name,
        description=body.description,
        aws_services=body.aws_services,
        tags=body.tags,
        framework=body.framework,
    )
    return joined_template_view(connection_id, view)


@router.delete("/{name}", status_code=204)
async def delete_template(
    name: str,
    connection_id: str,
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.ADMIN)),
):
    """Deregister a template: the catalog entry goes, the repository behind it stays."""
    get_github_template_service().delete_template(connection_id, name)
    return Response(status_code=204)


def _infra_view_from_record(record: InfraTemplateRecord) -> InfraTemplateView:
    return InfraTemplateView(
        id=record.id,
        name=record.name,
        description=record.description,
        platform=record.platform,
        iac_type=record.iac_type,
        build_kind=record.build_kind,
        repo_name=record.repo_name,
        version=record.version,
        updated_at=record.updated_at or record.created_at,
    )


def _joined_with_infra(connection_id: str, views: List[TemplateView]) -> List[TemplateView]:
    """Fill each view's ``platform`` and ``build_kind`` from the infra record its
    ``infra_template_id`` names: the derived compatibility fact the picker filters on, read with
    one ``list()`` for the whole page.

    Best-effort, unlike the infra list route's strict 503: the join enriches an answer that
    already read fine, and the fallback is the pre-catalog truth (an AgentCore container template
    on AWS). Every fallback is logged, an empty catalog with its own line, because a connection
    whose rollout never ran would otherwise fall back silently."""
    if not views:
        return views

    unreadable = False
    try:
        by_id = {r.id: r for r in get_infra_template_registry().list(connection_id)}
    except InfraTemplateRegistryError:
        unreadable = True
        by_id = {}
        logger.warning(
            "[github-templates] infra catalog unreadable for connection %s — %d template "
            "view(s) fall back to %s/%s",
            connection_id,
            len(views),
            _FALLBACK_PLATFORM,
            _FALLBACK_BUILD_KIND,
            exc_info=True,
        )

    if not by_id and not unreadable:
        logger.warning(
            "[github-templates] no infra templates registered for connection %s — %d template "
            "view(s) fall back to %s/%s (has the rollout run?)",
            connection_id,
            len(views),
            _FALLBACK_PLATFORM,
            _FALLBACK_BUILD_KIND,
        )

    joined: List[TemplateView] = []
    for view in views:
        infra = by_id.get(view.infra_template_id)
        if infra is None and by_id:
            logger.warning(
                "[github-templates] template %s names unknown infra template %s — falling back to %s/%s",
                view.name,
                view.infra_template_id,
                _FALLBACK_PLATFORM,
                _FALLBACK_BUILD_KIND,
            )
        joined.append(
            view.model_copy(
                update={
                    "platform": infra.platform if infra else _FALLBACK_PLATFORM,
                    "build_kind": infra.build_kind if infra else _FALLBACK_BUILD_KIND,
                }
            )
        )
    return joined


def joined_template_view(connection_id: str, view: TemplateView) -> TemplateView:
    """Join a single view, for the create, patch and adopt responses: every route returning a
    ``TemplateView`` must return the same view, or a PATCH on a config template would answer a
    container badge beside its infra template id. Public because the adopt route lives in
    ``connections.py``."""
    return _joined_with_infra(connection_id, [view])[0]
