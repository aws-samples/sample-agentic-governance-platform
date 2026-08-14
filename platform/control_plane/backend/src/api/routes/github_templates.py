"""Template catalog admin API (E22/T2) — list / upload-zip / patch / deregister.

The connection-scoped successor to ``ops_templates.py`` (S3 + DDB). Structural clone of
``connections.py`` / ``ops_templates.py``: the lazy ``_svc`` / ``get_github_template_service()``
singleton (tests patch ``_svc`` directly so this never runs against live GitHub/AWS), and the
FIXED-``detail`` convention in ``_raise_template_error`` — NEVER ``str(err)``, so a raw GitHub
or store body (wrapped by the service into a SAFE ``GitHubTemplateError``) can never leak to
the client.

E28B/T2: the catalog is AGP's own ``template`` registry partition, not a GitHub
``is_template`` query. Consequently **DELETE deregisters** — it removes the catalog entry and
leaves the repository in place (a registered ``source_url`` may name a public repo or a mirror
AGP does not own). The route's semantics moved, so its 204 must not be read as "the repo is
gone"; the console copy is what tells the operator which it was.

Every operation is scoped to a ``connection_id`` (path/query for GET/PATCH/DELETE, a form field
for the multipart POST). RBAC: reads (``GET``) require OPERATOR; writes (``POST``/``PATCH``/
``DELETE``) require ADMIN — mirrors the ops-template precedent.
"""

import logging
from typing import List, Optional

from fastapi import APIRouter, File, Form, HTTPException, Response, UploadFile
from fastapi import Depends as RBACDepends
from pydantic import BaseModel

from core.config import settings
from core.rbac import Principal, Role, current_principal, require_role
from services.connection_service import ConnectionService
from services.github_repo_service import GitHubRepoService
from services.github_template_service import (
    GitHubTemplateError,
    GitHubTemplateService,
    TemplateView,
)
from services.template_registry import TemplateRegistry

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/github-templates", tags=["github-templates"])

_svc: Optional[GitHubTemplateService] = None

# GitHubTemplateError.kind → HTTP status. FIXED detail literals (never str(err), which could
# carry a raw GitHub/store body): invalid_zip|invalid_input → 422, not_found → 404,
# github_error → 502, store_error → 503 (the catalog read is RETRYABLE — an unreadable
# partition is a transient store fault, not a client error, and 503 is what tells the console
# to retry rather than render an empty catalog).
_ERROR_STATUS = {
    "invalid_zip": 422,
    "invalid_input": 422,
    "not_found": 404,
    "github_error": 502,
    "store_error": 503,
}
_ERROR_DETAIL = {
    "invalid_zip": "Invalid template zip",
    "invalid_input": "Invalid template metadata",
    "not_found": "Template not found",
    "github_error": "GitHub template operation failed",
    "store_error": "Template catalog is temporarily unavailable",
}


class TemplatePatch(BaseModel):
    """Write-only PATCH body — editable metadata only (never the name)."""

    description: Optional[str] = None
    aws_services: Optional[List[str]] = None
    tags: Optional[List[str]] = None
    framework: Optional[str] = None

    class Config:
        extra = "ignore"


def get_github_template_service() -> GitHubTemplateService:
    """Lazy ``GitHubTemplateService`` singleton built from ``settings``.

    Wires a real ``GitHubRepoService`` (per-request Bearer auth, still needed for the upload's
    repo create) + a ``ConnectionService`` (resolves org/base_url/token per connection) + the
    ``TemplateRegistry``, which is the ``template`` partition of the EXISTING projects table
    (``PROJECTS_TABLE_NAME`` — no new table, no new env var). Tests patch ``_svc`` directly
    with a fake service so this never runs against live GitHub / AWS.
    """
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


def _raise_template_error(err: GitHubTemplateError) -> None:
    """Map a GitHubTemplateError to an HTTPException with a FIXED detail literal — a raw
    GitHub/store value never reaches the client."""
    status = _ERROR_STATUS.get(err.kind, 422)
    detail = _ERROR_DETAIL.get(err.kind, "Template operation failed")
    raise HTTPException(status_code=status, detail=detail)


@router.get("", response_model=List[TemplateView])
async def list_templates(
    connection_id: str,
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    svc = get_github_template_service()
    try:
        return svc.list_templates(connection_id)
    except GitHubTemplateError as err:
        _raise_template_error(err)


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
    svc = get_github_template_service()
    zip_bytes = await file.read()
    try:
        return svc.upload_template(
            connection_id,
            zip_bytes=zip_bytes,
            name=name,
            description=description,
            framework=framework,
            aws_services=aws_services,
            tags=tags,
            # The registrant, from the VALIDATED principal — never a body/form value. Same
            # `email or oid` precedence the other create routes use.
            created_by=principal.email or principal.oid or "",
        )
    except GitHubTemplateError as err:
        _raise_template_error(err)


@router.patch("/{name}", response_model=TemplateView)
async def patch_template(
    name: str,
    connection_id: str,
    body: TemplatePatch,
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.ADMIN)),
):
    svc = get_github_template_service()
    try:
        return svc.patch_template(
            connection_id,
            name,
            description=body.description,
            aws_services=body.aws_services,
            tags=body.tags,
            framework=body.framework,
        )
    except GitHubTemplateError as err:
        _raise_template_error(err)


@router.delete("/{name}", status_code=204)
async def delete_template(
    name: str,
    connection_id: str,
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.ADMIN)),
):
    """DEREGISTER a template — remove the catalog entry only.

    The repository behind the entry is NOT deleted (E28B/T2): ``source_url`` may name a public
    repo or a mirror AGP does not own. A 204 here means "no longer in the catalog", never "the
    repository was removed"."""
    svc = get_github_template_service()
    try:
        svc.delete_template(connection_id, name)
    except GitHubTemplateError as err:
        _raise_template_error(err)
    return Response(status_code=204)
