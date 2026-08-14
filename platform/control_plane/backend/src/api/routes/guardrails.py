"""Guardrail template CRUD API routes.

RBAC (E34/T5): presets/list/get/metrics = VIEWER; create/update/publish = OPERATOR;
DELETE = ADMIN — deleting a template destroys a live AWS resource (the Bedrock guardrail
itself, via `guardrail_service.delete_template`), which is a lifecycle act rather than an
ordinary mutation. It is NOT an enforcement claim: nothing wires a template's guardrail_id
into a runtime, and neither `models/agent.py` nor `models/mcp_server.py` carries a guardrail
field. Gating is per-endpoint via trailing `RBACDepends` params, the `mcp_servers.py` idiom.

`created_by` comes from the validated `current_principal`, never a hardcoded "user".

Multi-tenancy (Epic 24, retrofitted in E34/T5): a `tenant_ctx` per-request dependency
(delegating to `users.get_tenant_ctx` — the ONE resolver-singleton accessor) gates every
`{template_id}` route via `_load_visible_guardrail` BEFORE any side effect, and
post-filters `list` results. Create verifies the target tenant exists (400) and that the
caller is a member of it (403 — no resource exists yet to conceal).

Route ORDER matters: `/presets` must stay declared before `/{template_id}`, or "presets"
resolves as a template id and the endpoint silently answers 404 instead.
"""

from fastapi import APIRouter, HTTPException, Query
from fastapi import Depends as RBACDepends
from typing import Optional, List
import logging

from models.guardrail import (
    GuardrailTemplate,
    GuardrailTemplateCreate,
    GuardrailTemplateUpdate,
    GuardrailStatus,
    GuardrailPreset,
    GuardrailMetrics,
)
from services.guardrail_service import GuardrailService
from services.tenant_resolver import TenantContext, visible
from core.config import settings
from core.rbac import Principal, Role, current_principal, require_role

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
    """Delegate to the ONE resolver-singleton accessor (E24/T5).

    ``users.py`` owns the lazy ``TenantResolver`` singleton (``_tenant_resolver`` /
    ``get_tenant_resolver()``); this is a thin re-export so guardrails.py has its
    own per-request ``tenant_ctx`` dependency WITHOUT keeping a second resolver
    copy — tests patch ``api.routes.users._tenant_resolver`` and both /users/me and
    every route here observe it. Imported lazily to avoid an import cycle at module
    load (mirrors ``mcp_servers.get_tenant_ctx``).
    """
    from api.routes.users import get_tenant_ctx as _users_get_tenant_ctx

    return await _users_get_tenant_ctx(principal)


async def _load_visible_guardrail(
    template_id: str, ctx: TenantContext
) -> GuardrailTemplate:
    """Load a template by id and gate it on tenant visibility BEFORE any side effect.

    ONE helper every ``{template_id}`` route calls — missing an endpoint is this
    retrofit's stated failure mode. A missing OR not-visible template raises the SAME
    404 literal, byte-identical between the two cases: a distinguishable response
    would confirm that a foreign tenant's guardrail exists.
    """
    template = get_service().get_template(template_id)
    if not template or not visible(ctx, template.tenant_id):
        raise HTTPException(status_code=404, detail="Guardrail template not found")
    return template


# --- Presets ---

@router.get("/presets", response_model=List[GuardrailPreset])
async def list_presets(
    _=RBACDepends(require_role(Role.VIEWER)),
):
    """Get pre-built guardrail configuration presets"""
    svc = get_service()
    return svc.get_presets()


# --- CRUD ---

@router.post("", response_model=GuardrailTemplate, status_code=201)
async def create_guardrail(
    req: GuardrailTemplateCreate,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    """Create a new guardrail template and provision it in Bedrock"""
    # Multi-tenancy (E24) — tenant_id must exist, then (for a non-global caller) must be
    # one of the caller's own memberships. No resource exists yet, so 403 (not 404) is
    # correct here — unlike the visibility-gated routes below.
    from api.routes.tenants import get_tenant_service
    from services.tenant_service import TenantError

    try:
        get_tenant_service().get(req.tenant_id)
    except TenantError:
        raise HTTPException(status_code=400, detail="unknown tenant")
    if not ctx.is_global and req.tenant_id not in ctx.tenant_ids:
        raise HTTPException(status_code=403, detail="tenant not permitted")

    svc = get_service()
    template = svc.create_template(req, created_by=principal.email or principal.oid)
    if template.status == GuardrailStatus.FAILED:
        # A FIXED literal, like every other HTTPException in this file (E34/T5b). Interpolating
        # the last status-history message put the service's AWS failure text in the response
        # body — a botocore AccessDeniedException names the account id, the execution role and
        # the instance id. The service logs the real cause; the caller gets the outcome.
        raise HTTPException(status_code=502, detail="Guardrail creation failed")
    return template


@router.get("", response_model=List[GuardrailTemplate])
async def list_guardrails(
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.VIEWER)),
    status: Optional[str] = Query(default=None),
):
    """List all guardrail templates, optionally filtered by status"""
    svc = get_service()
    status_filter = GuardrailStatus(status) if status else None
    records = svc.list_templates(status=status_filter)
    # Tenant post-filter (E24) — filtering lands in the ROUTE, not the service, which
    # keeps TenantContext out of the service layer (the mcp_servers.py:272 precedent).
    return [t for t in records if visible(ctx, t.tenant_id)]


@router.get("/{template_id}", response_model=GuardrailTemplate)
async def get_guardrail(
    template_id: str,
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.VIEWER)),
):
    """Get a single guardrail template by ID"""
    return await _load_visible_guardrail(template_id, ctx)


@router.put("/{template_id}", response_model=GuardrailTemplate)
async def update_guardrail(
    template_id: str,
    req: GuardrailTemplateUpdate,
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    """Update a guardrail template configuration"""
    await _load_visible_guardrail(template_id, ctx)
    svc = get_service()
    template = svc.update_template(template_id, req)
    if not template:
        raise HTTPException(status_code=404, detail="Guardrail template not found")
    return template


@router.delete("/{template_id}", response_model=GuardrailTemplate)
async def delete_guardrail(
    template_id: str,
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.ADMIN)),
):
    """Delete a guardrail template and its Bedrock resource"""
    await _load_visible_guardrail(template_id, ctx)
    svc = get_service()
    template = svc.delete_template(template_id)
    if not template:
        raise HTTPException(status_code=404, detail="Guardrail template not found")
    return template


@router.post("/{template_id}/publish", response_model=GuardrailTemplate)
async def publish_guardrail(
    template_id: str,
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    """Publish a new version of the guardrail in Bedrock"""
    await _load_visible_guardrail(template_id, ctx)
    svc = get_service()
    template = svc.publish_version(template_id)
    if not template:
        # Deliberately a DIFFERENT literal from the tenant/missing 404 above: the
        # visibility gate has already passed, so this describes a genuinely different
        # state — a visible draft that was never provisioned in Bedrock.
        raise HTTPException(status_code=404, detail="Guardrail template not found or has no Bedrock resource")
    return template


# --- Observability ---

@router.get("/{template_id}/metrics", response_model=GuardrailMetrics)
async def get_guardrail_metrics(
    template_id: str,
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.VIEWER)),
    hours: int = Query(default=24, ge=1, le=168),
):
    """Get observability metrics for a guardrail"""
    template = await _load_visible_guardrail(template_id, ctx)
    svc = get_service()
    if not template.guardrail_id:
        raise HTTPException(status_code=400, detail="Guardrail has no Bedrock resource (still in draft)")

    return svc.get_metrics(template.guardrail_id, hours=hours)
