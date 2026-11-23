"""Guardrail template API: presets, CRUD, publish and metrics.

Roles per endpoint: presets, list, get and metrics are VIEWER; create, update and publish are
OPERATOR; delete is ADMIN, because it destroys the Bedrock guardrail behind the template. Gating
is per endpoint through trailing ``RBACDepends`` parameters. Nothing wires a template's guardrail
into a runtime; this is template lifecycle, not enforcement.

Tenancy: a per-request ``tenant_ctx`` gates every ``{template_id}`` route through
:func:`_load_visible_guardrail` before any side effect and post-filters the list. Create checks
that the target tenant exists and that the caller is a member of it; no resource exists yet, so
that refusal is a 403. ``created_by`` is the validated principal's identity.
"""

import logging
from typing import List, Optional

from fastapi import APIRouter, Query
from fastapi import Depends as RBACDepends

from core.config import settings
from core.rbac import Principal, Role, current_principal, require_role
from models.guardrail import (
    GuardrailMetrics,
    GuardrailPreset,
    GuardrailStatus,
    GuardrailTemplate,
    GuardrailTemplateCreate,
    GuardrailTemplateUpdate,
)
from services.guardrail_service import GuardrailError, GuardrailService
from services.tenant_resolver import TenantContext, require_member, visible

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/guardrails", tags=["guardrails"])

_svc = None


def get_service() -> GuardrailService:
    global _svc
    if _svc is None:
        _svc = GuardrailService(
            table_name=settings.GUARDRAILS_TABLE_NAME,
            region=settings.AWS_REGION,
        )
    return _svc


async def get_tenant_ctx(
    principal: Principal = RBACDepends(current_principal),
) -> TenantContext:
    """Delegates to the one tenant-resolver singleton ``users.py`` owns, so tests that patch it
    reach every route here too."""
    from api.routes.users import get_tenant_ctx as _users_get_tenant_ctx

    return await _users_get_tenant_ctx(principal)


async def _load_visible_guardrail(template_id: str, ctx: TenantContext) -> GuardrailTemplate:
    """Load a template and gate it on tenant visibility before any side effect. A missing and a
    foreign template answer the same 404, so the response confirms nothing about a foreign
    tenant's guardrails."""
    template = get_service().get_template(template_id)
    if not template or not visible(ctx, template.tenant_id):
        raise GuardrailError(f"guardrail template {template_id} is missing or not visible", kind="not_found")
    return template


@router.get("/presets", response_model=List[GuardrailPreset])
async def list_presets(
    _=RBACDepends(require_role(Role.VIEWER)),
):
    return get_service().get_presets()


@router.post("", response_model=GuardrailTemplate, status_code=201)
async def create_guardrail(
    req: GuardrailTemplateCreate,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    """Create a template and provision its Bedrock guardrail. A failed provisioning answers a
    fixed 502; the service's failure text stays in the log."""
    from api.routes.tenants import get_tenant_service

    get_tenant_service().get(req.tenant_id)
    require_member(ctx, req.tenant_id)

    template = get_service().create_template(req, created_by=principal.email or principal.oid or "")
    if template.status == GuardrailStatus.FAILED:
        raise GuardrailError(
            f"guardrail template {template.template_id} provisioning ended FAILED", kind="creation_failed"
        )
    return template


@router.get("", response_model=List[GuardrailTemplate])
async def list_guardrails(
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.VIEWER)),
    status: Optional[str] = Query(default=None),
):
    status_filter = GuardrailStatus(status) if status else None
    records = get_service().list_templates(status=status_filter)
    return [t for t in records if visible(ctx, t.tenant_id)]


@router.get("/{template_id}", response_model=GuardrailTemplate)
async def get_guardrail(
    template_id: str,
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.VIEWER)),
):
    return await _load_visible_guardrail(template_id, ctx)


@router.put("/{template_id}", response_model=GuardrailTemplate)
async def update_guardrail(
    template_id: str,
    req: GuardrailTemplateUpdate,
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    await _load_visible_guardrail(template_id, ctx)
    template = get_service().update_template(template_id, req)
    if not template:
        raise GuardrailError(f"guardrail template {template_id} vanished before the update", kind="not_found")
    return template


@router.delete("/{template_id}", response_model=GuardrailTemplate)
async def delete_guardrail(
    template_id: str,
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.ADMIN)),
):
    await _load_visible_guardrail(template_id, ctx)
    template = get_service().delete_template(template_id)
    if not template:
        raise GuardrailError(f"guardrail template {template_id} vanished before the delete", kind="not_found")
    return template


@router.post("/{template_id}/publish", response_model=GuardrailTemplate)
async def publish_guardrail(
    template_id: str,
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    """Publish a new version of the guardrail in Bedrock. A visible draft that was never
    provisioned answers its own 404 literal, distinct from the visibility gate's."""
    await _load_visible_guardrail(template_id, ctx)
    template = get_service().publish_version(template_id)
    if not template:
        raise GuardrailError(
            f"guardrail template {template_id} has nothing to publish", kind="not_publishable"
        )
    return template


@router.get("/{template_id}/metrics", response_model=GuardrailMetrics)
async def get_guardrail_metrics(
    template_id: str,
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.VIEWER)),
    hours: int = Query(default=24, ge=1, le=168),
):
    template = await _load_visible_guardrail(template_id, ctx)
    if not template.guardrail_id:
        raise GuardrailError(f"guardrail template {template_id} is a draft", kind="no_bedrock_resource")

    return get_service().get_metrics(
        template.guardrail_id,
        hours=hours,
        guardrail_arn=template.guardrail_arn,
        guardrail_version=template.guardrail_version or "DRAFT",
    )
