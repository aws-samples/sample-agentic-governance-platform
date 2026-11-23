"""Handlers the tenant census in `tests/test_tenant_scoping_per_domain.py` must classify.

Never imported and never collected: the self-tests parse this file and assert the census's verdict
per handler by name, so a case that stops being parsed fails the test that names it. The imports are
the route modules' own and resolve under `pythonpath = ["src"]`, but for the defaults fixture's
`GLOBAL_CTX` that `ctx_rebound_by_an_import` imports as `ctx`; nothing here runs.
"""

import logging
from dataclasses import replace

from fastapi import APIRouter, HTTPException, Query
from fastapi import Depends as RBACDepends
from pydantic import BaseModel

from api.routes.agents import _agent_repository as _scoped_repository
from api.routes.users import get_tenant_ctx
from core.rbac import Role, require_role
from services.tenant_resolver import TenantContext, require_member, visible

logger = logging.getLogger(__name__)

router = APIRouter()


class WidgetLookup(BaseModel):
    widget_id: str


class WidgetService:
    def get(self, widget_id: str):
        raise NotImplementedError


class WidgetCreate(BaseModel):
    tenant_id: str
    name: str


def get_widget_service():
    raise NotImplementedError


def get_part_service():
    raise NotImplementedError


def get_tenant_service():
    raise NotImplementedError


def get_gadget_service():
    raise NotImplementedError


def get_agent_service():
    raise NotImplementedError


def get_logger():
    raise NotImplementedError


class WidgetPolicy:
    @staticmethod
    def visible(ctx: TenantContext, tenant_id: str) -> bool:
        raise NotImplementedError


class ForgedContext(TenantContext, Exception):
    pass


def _gate(scope: TenantContext):
    return scope


async def _load_visible_widget(widget_id: str, ctx: TenantContext):
    widget = get_widget_service().get(widget_id)
    if not widget or not visible(ctx, widget.tenant_id):
        raise HTTPException(status_code=404, detail="Widget not found")
    return widget


@router.get("/widgets/{widget_id}")
async def get_widget(
    widget_id: str,
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
):
    return await _load_visible_widget(widget_id, ctx)


@router.get("/widgets/{widget_id}/unscoped")
async def get_widget_unscoped(
    widget_id: str,
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    widget = get_widget_service().get(widget_id)
    if not widget:
        raise HTTPException(status_code=404, detail="Widget not found")
    return widget


@router.post("/widgets/{widget_id}/archive", status_code=204)
async def archive_widget(
    widget_id: str,
    _=RBACDepends(require_role(Role.ADMIN)),
):
    get_widget_service().archive(widget_id)


@router.get("/admin/tenants/{tenant_id}")
async def get_tenant_admin(
    tenant_id: str,
    _=RBACDepends(require_role(Role.ADMIN)),
):
    return get_tenant_service().get(tenant_id)


@router.get("/widgets/{widget_id}/parts/{part_id}")
async def get_widget_part(
    widget_id: str,
    part_id: str,
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
):
    await _load_visible_widget(widget_id, ctx)
    part = get_part_service().get(part_id)
    if not part or part.widget_id != widget_id:
        raise HTTPException(status_code=404, detail="Part not found")
    return part


async def _load_visible_gadget(id: str, ctx: TenantContext):
    gadget = get_gadget_service().get(id)
    if not gadget or not visible(ctx, gadget.tenant_id):
        raise HTTPException(status_code=404, detail="Gadget not found")
    return gadget


@router.get("/gadgets/{id}")
async def get_gadget_raw(
    id: str,
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
):
    gadget = get_gadget_service().get(id)
    if not gadget:
        raise HTTPException(status_code=404, detail="Gadget not found")
    return gadget


@router.get("/widgets/{widget_id}/two-step")
async def two_step_read_with_ctx(
    widget_id: str,
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
):
    svc = get_widget_service()
    return svc.get(widget_id)


@router.get("/widgets")
async def query_id_without_ctx(
    widget_id: str = Query(...),
    _=RBACDepends(require_role(Role.VIEWER)),
):
    svc = get_widget_service()
    return svc.get(widget_id)


@router.post("/widgets/lookup")
async def body_id_without_ctx(
    req: WidgetLookup,
    _=RBACDepends(require_role(Role.VIEWER)),
):
    return get_widget_service().get(req.widget_id)


@router.get("/widgets/{widget_id}/logged")
async def ctx_only_logged(
    widget_id: str,
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
):
    logger.info("caller %s", ctx)
    return get_widget_service().get(widget_id)


@router.get(path="/widgets/{widget_id}/keyword")
async def path_keyword_without_ctx(
    widget_id: str,
    _=RBACDepends(require_role(Role.VIEWER)),
):
    svc = get_widget_service()
    return svc.get(widget_id)


@router.get("/agents/{agent_id}/grants/drift")
async def grants_shape_two_step(
    agent_id: str,
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.VIEWER)),
):
    registry = get_agent_service()
    agent = registry.get(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    return agent


@router.get("/widgets/{widget_id}/aliased")
async def aliased_ctx(
    widget_id: str,
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
):
    scope = ctx
    return await _load_visible_widget(widget_id, scope)


@router.post("/widgets", status_code=201)
async def create_with_require_member(
    req: WidgetCreate,
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    get_tenant_service().get(req.tenant_id)
    require_member(ctx, req.tenant_id)
    return get_widget_service().create(req)


@router.get("/widgets/{widget_id}/hinted")
async def admin_name_in_a_query_default(
    widget_id: str,
    hint: str = Query(default=Role.ADMIN.name),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    return get_widget_service().get(widget_id)


@router.get("/widgets/{widget_id}/forged")
async def forged_global_ctx(
    widget_id: str,
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
):
    return await _load_visible_widget(widget_id, replace(ctx, is_global=True))


@router.post("/widgets/lookup/annotated")
async def annotated_receiver_body_id(
    req: WidgetLookup,
    _=RBACDepends(require_role(Role.VIEWER)),
):
    svc: WidgetService = get_widget_service()
    return svc.get(req.widget_id)


@router.get("/widgets/{widget_id}/gated")
async def gate_scoping_only_elsewhere(
    widget_id: str,
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
):
    _gate(ctx)
    return get_widget_service().get(widget_id)


@router.get("/widgets/{widget_id}/rebound")
async def rebound_ctx(
    widget_id: str,
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
):
    ctx = replace(ctx, is_global=True)
    return await _load_visible_widget(widget_id, ctx)


@router.get("/widgets/{widget_id}/imported")
async def ctx_rebound_by_an_import(
    widget_id: str,
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
):
    from tests.fixtures.tenant_context.defaults_fixture import GLOBAL_CTX as ctx

    return await _load_visible_widget(widget_id, ctx)


@router.get("/widgets/{widget_id}/caught")
async def ctx_rebound_by_except_as(
    widget_id: str,
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
):
    try:
        return await _load_visible_widget(widget_id, ctx)
    except ForgedContext as ctx:
        return await _load_visible_widget(widget_id, ctx)


@router.get("/widgets/{widget_id}/logger")
async def ctx_logged_through_a_logger_getter(
    widget_id: str,
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
):
    log = get_logger()
    log.info("caller %s", ctx)
    get_logger().debug("caller %s", ctx)
    return get_widget_service().get(widget_id)


@router.get("/widgets/{widget_id}/policy")
async def visible_on_a_non_module(
    widget_id: str,
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
):
    widget = get_widget_service().get(widget_id)
    if not WidgetPolicy.visible(ctx, widget.tenant_id):
        raise HTTPException(status_code=404, detail="Widget not found")
    return widget


@router.get("/widgets/{widget_id}/discarded")
async def visible_result_discarded(
    widget_id: str,
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
):
    widget = get_widget_service().get(widget_id)
    visible(ctx, widget.tenant_id)
    return widget


@router.get("/widgets/{widget_id}/nested")
async def scoping_only_in_nested_scopes(
    widget_id: str,
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
):
    def checked() -> bool:
        return visible(ctx, widget_id)

    async def loaded():
        return await _load_visible_widget(widget_id, ctx)

    sorted([widget_id], key=lambda _: visible(ctx, widget_id))
    return get_widget_service().get(widget_id)


@router.get("/agents/{agent_id}/repository")
async def repository_through_an_imported_scoping_function(
    agent_id: str,
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
):
    return await _scoped_repository(agent_id, ctx)


@router.post("/widgets/lookup/deferred")
async def body_id_read_in_a_lambda(
    req: WidgetLookup,
    _=RBACDepends(require_role(Role.VIEWER)),
):
    return sorted([req], key=lambda r: get_widget_service().get(r.widget_id))


@router.post("/widgets/lookup/local")
async def body_id_through_a_local(
    req: WidgetLookup,
    _=RBACDepends(require_role(Role.VIEWER)),
):
    target = req.widget_id
    return get_widget_service().get(target)


WIDGET_BY_NAME = "/widgets/by-name/{name}"


@router.get(WIDGET_BY_NAME)
async def path_in_a_constant(
    name: str,
    _=RBACDepends(require_role(Role.VIEWER)),
):
    return get_widget_service().get_by_name(name)
