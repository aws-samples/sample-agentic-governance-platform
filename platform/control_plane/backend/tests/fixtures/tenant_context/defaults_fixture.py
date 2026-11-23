"""Parameters the default census in `tests/test_tenant_context_never_defaults.py` must classify.

Never imported and never collected: the self-test parses this file and asserts the census names
exactly the defaulted shapes, by function, so a shape that stops being parsed fails it. `kw_none`
defaults a keyword-only `ctx` to None; `global_default` defaults it to a module constant;
`positional_none` takes it as a positional parameter with a default. The next five name the
parameter `scope`, so the annotation alone flags them: `optional_none` annotates it
`Optional[TenantContext]`, `union_none` `TenantContext | None`, `quoted_none` the same in a string,
`renamed_default` a bare `TenantContext` defaulted to the constant, and `qualified_default`
`tenant_resolver.TenantContext`. `wrapped_provider` defaults a keyword-only `ctx` to `Query` over
`get_tenant_ctx`, a call that is not a dependency. Three routes default `ctx` to a dependency other than
the tenant scope: `lambda_dependency` to `Depends` over a lambda, `other_provider` to
`RBACDepends` over another provider and `bare_dependency` to a bare `Depends()`. Three controls
pass: `required_kw` has no default, `scoped_route` defaults to `RBACDepends(get_tenant_ctx)` and
`plain_route` to `Depends(get_tenant_ctx)`. The imports are the route modules' own and resolve
under `pythonpath = ["src"]`; nothing here runs.
"""

from typing import Optional

from fastapi import APIRouter, Depends, Query
from fastapi import Depends as RBACDepends

from api.routes.users import get_project_ctx, get_tenant_ctx
from services import tenant_resolver
from services.tenant_resolver import TenantContext

router = APIRouter()

GLOBAL_CTX = TenantContext(is_global=True, tenant_ids=frozenset(), tenants=())


class WidgetService:
    def kw_none(self, widget_id: str, *, ctx=None) -> None:
        raise NotImplementedError

    def global_default(self, widget_id: str, *, ctx=GLOBAL_CTX) -> None:
        raise NotImplementedError

    def positional_none(self, widget_id: str, ctx=None) -> None:
        raise NotImplementedError

    def optional_none(self, widget_id: str, *, scope: Optional[TenantContext] = None) -> None:
        raise NotImplementedError

    def union_none(self, widget_id: str, *, scope: TenantContext | None = None) -> None:
        raise NotImplementedError

    def quoted_none(self, widget_id: str, *, scope: "TenantContext | None" = None) -> None:
        raise NotImplementedError

    def renamed_default(self, widget_id: str, *, scope: TenantContext = GLOBAL_CTX) -> None:
        raise NotImplementedError

    def qualified_default(self, widget_id: str, *, scope: tenant_resolver.TenantContext = GLOBAL_CTX) -> None:
        raise NotImplementedError

    def wrapped_provider(self, widget_id: str, *, ctx=Query(get_tenant_ctx)) -> None:
        raise NotImplementedError

    def required_kw(self, widget_id: str, *, ctx: TenantContext) -> None:
        raise NotImplementedError


@router.get("/widgets/{widget_id}")
async def scoped_route(widget_id: str, ctx: TenantContext = RBACDepends(get_tenant_ctx)) -> None:
    raise NotImplementedError


@router.get("/gadgets/{gadget_id}")
async def plain_route(gadget_id: str, ctx: TenantContext = Depends(get_tenant_ctx)) -> None:
    raise NotImplementedError


@router.get("/gizmos/{gizmo_id}")
async def lambda_dependency(gizmo_id: str, ctx: TenantContext = Depends(lambda: GLOBAL_CTX)) -> None:
    raise NotImplementedError


@router.get("/doohickeys/{doohickey_id}")
async def other_provider(doohickey_id: str, ctx: TenantContext = RBACDepends(get_project_ctx)) -> None:
    raise NotImplementedError


@router.get("/whatsits/{whatsit_id}")
async def bare_dependency(whatsit_id: str, ctx: TenantContext = Depends()) -> None:
    raise NotImplementedError
