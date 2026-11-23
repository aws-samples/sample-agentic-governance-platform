"""A foreign tenant's resource in a new domain looks absent: every id-bearing route handler hands the
caller's ``TenantContext`` to a scoping call.

The census reads every route handler under ``src/api/routes/`` (a ``get``, ``post``, ``put``,
``patch``, ``delete`` or ``api_route`` decorator, its path a string literal, positional or ``path=``;
outside the exempt set below, any other path is a finding). A handler is id-bearing when its path has a
``{param}``, when a parameter is named ``id`` or ``*_id`` (a query id), or when it calls a method on a
receiver (a ``get_*()`` call, or a name bound to a zero-argument call) with an argument whose last
segment is ``id`` or ``*_id`` (``req.agent_id``, ``body.tenant_id``) or a local bound from one. Outside
``TENANT_WIDE_MODULES``, each with its reason, and the handlers gated ``require_role(Role.ADMIN)``, an
id-bearing handler declares a ``TenantContext``, never rebinds that name (an assignment, an import, an
``except`` or ``match`` capture, a ``def`` or ``class``, a ``nonlocal`` declaration, or a ``global`` one,
which fails closed: a nested ``global ctx`` names the module's ``ctx``, not the handler's, and is flagged
all the same), and passes it itself into a scoping call or into a method call on a service, a
``*_service()`` call or a name bound to one (``svc.build(ctx=ctx)``). A scoping call is a visibility
helper, ``visible`` whose result is used, ``require_member``, any ``_load_visible*``, or a route-module
function that calls one, resolved in its own module through a bare name, a ``from api.routes.<module>``
import or a module alias, transitively (``evals._gate``, ``studio._gate``); a call inside a nested
function or a lambda does not count. A ``ctx`` handed only to a log call or another non-service receiver,
or to a scoping call only under an alias, is a finding. ``BY_DESIGN_PRINCIPAL_SCOPED_HANDLERS`` holds the
handlers the principal scopes instead, each with its reason, and equals the findings it excuses. The
endpoints the built app serves, ``main.root`` aside, are the handlers the census reads, so a route added
through ``add_api_route`` or a decorator spelling the census cannot parse fails the cross-check.

Not held, said so: Y, a scoping call on one id while the read uses another
(``await _load_visible_agent(agent_id, ctx)`` then ``get_service().get(other_id)``), which is also whether
a child record read after the parent's helper belongs to that parent, because reading it strictly flags
every child read that checks its parent inline; a ``ctx`` handed to one service call beside an unscoped
read on another (``get_audit_service().record(ctx)`` then ``get_service().get(agent_id)``), because the
service arm accepts ``svc.build(ctx=ctx)`` before a read and tying the ``ctx`` to the id's call would flag
every such handler; a scoping call that never runs (``if False:``) and a ``visible`` result bound and
never read; a walrus in a nested scope's header, a nested ``def``'s default or decorator or a lambda's
default (``def inner(x=(ctx := None))``, ``lambda x=(ctx := None): x``, ``@(ctx := …)``), because the
census's own scope stops at the nested ``def`` or ``lambda`` and reads neither its defaults nor its
decorators; a create that never reads its tenant id (``get_service().create(req)``); a list route with no
id (``list_guardrails`` leaked at ``267cb69b^``); field-level redaction inside a visible resource
(``b8903f21``, a different class); a receiver bound any other way (``svc = await get_x()``, an attribute,
a parameter); an id nested in an expression (``svc.get(str(agent_id))``, ``body["agent_id"]``) or passed
through a second local; a scoping function that takes ``ctx`` and ignores it; and a
``require_role(Role.ADMIN)`` nested inside another default, which still gates. Those stay with the
per-domain tests and the reviewer.
"""

import ast
import os
import re
import shutil
from collections import namedtuple
from pathlib import Path

from fastapi.routing import APIRoute, _IncludedRouter
from starlette.routing import BaseRoute

REPO_ROOT = (
    Path(os.environ["AGP_REPO_ROOT"])
    if "AGP_REPO_ROOT" in os.environ
    else Path(__file__).resolve().parents[4]
)
BACKEND = REPO_ROOT / "platform/control_plane/backend"
SRC = BACKEND / "src"
ROUTES = SRC / "api/routes"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "tenant_scoping"

Finding = namedtuple("Finding", "key message")
Handler = namedtuple(
    "Handler",
    "file name line route path_params id_params reads tenant_ctx admin_gated scoped rebinds aliases",
)

HTTP_VERBS = frozenset({"get", "post", "put", "patch", "delete", "api_route"})
PATH_PARAM_RE = re.compile(r"\{(\w+)(?::[^}]*)?\}")
SERVICE_GETTER_RE = re.compile(r"get_\w+")
SERVICE_RE = re.compile(r"\w*_service")
ROUTE_PACKAGE = "api.routes"
NESTED_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
NAMED_BINDINGS = (
    ast.ExceptHandler,
    ast.MatchAs,
    ast.MatchStar,
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.ClassDef,
)
ADMIN_ONLY_CLAIM = "every route is Role.ADMIN"
APP_LEVEL_ENDPOINTS = frozenset({("main", "root")})
ROUTE_MODULE_FLOOR = 20
ROUTE_HANDLER_FLOOR = 120
VISIBILITY_HELPER_FLOOR = 6

TENANT_WIDE_MODULES: dict[str, str] = {
    "health.py": "unauthenticated liveness and readiness; no resource id",
    "users.py": "/users/me reads the caller's own memberships",
    "tenants.py": "the platform-admin tenant directory; every route is Role.ADMIN",
    "users_admin.py": "the platform-admin user directory; every route is Role.ADMIN",
    "connections.py": "platform GitHub connections are not tenant-owned; Role.ADMIN writes, OPERATOR picker",
    "github_templates.py": "the platform template catalog is not tenant-owned",
    "github_link.py": "per-user links keyed by the caller's oid; {connection_id} is a platform connection",
    "builds.py": "a GitHub OIDC machine caller; the token's repository claim binds the agent (e37f148b)",
}
BY_DESIGN_PRINCIPAL_SCOPED_HANDLERS: dict[tuple[str, str], str] = {
    ("marketplace", "create_subscription"): (
        "principal-scoped: the service admits the caller only as the named agent's sponsor or grantee, "
        "and the product catalog is marketplace-wide, not tenant-owned"
    ),
}
VISIBILITY_HELPERS = (
    "_load_visible_agent",
    "_load_visible_mcp_server",
    "_load_visible_guardrail",
    "_load_visible_project",
    "_load_visible_repo",
    "_visible_scope_agents",
    "visible",
)
SCOPING_CALLS = (*VISIBILITY_HELPERS, "require_member")
HELPER_FOR_ID = {
    "agent_id": "_load_visible_agent",
    "mcp_id": "_load_visible_mcp_server",
    "mcp_server_id": "_load_visible_mcp_server",
    "template_id": "_load_visible_guardrail",
    "project_id": "_load_visible_project",
    "repo_id": "_load_visible_repo",
    "tenant_id": "require_member",
}
HELPER_FOR_METHOD = {
    "get_template": "_load_visible_guardrail",
    "get_project": "_load_visible_project",
    "get_repo": "_load_visible_repo",
}

FIXTURE_HANDLERS = {
    "_load_visible_widget",
    "get_widget",
    "get_widget_unscoped",
    "archive_widget",
    "get_tenant_admin",
    "get_widget_part",
    "_load_visible_gadget",
    "get_gadget_raw",
    "two_step_read_with_ctx",
    "query_id_without_ctx",
    "body_id_without_ctx",
    "ctx_only_logged",
    "path_keyword_without_ctx",
    "grants_shape_two_step",
    "aliased_ctx",
    "create_with_require_member",
    "admin_name_in_a_query_default",
    "forged_global_ctx",
    "annotated_receiver_body_id",
    "gate_scoping_only_elsewhere",
    "rebound_ctx",
    "ctx_rebound_by_an_import",
    "ctx_rebound_by_except_as",
    "ctx_logged_through_a_logger_getter",
    "visible_on_a_non_module",
    "visible_result_discarded",
    "scoping_only_in_nested_scopes",
    "repository_through_an_imported_scoping_function",
    "body_id_read_in_a_lambda",
    "body_id_through_a_local",
    "path_in_a_constant",
}
FIXTURE_HELPERS = {"_load_visible_widget", "_load_visible_gadget"}
SCOPING_AGENTS_MODULE = (
    "async def _gate(widget_id, ctx):\n    return await _load_visible_widget(widget_id, ctx)\n\n\n"
    "async def _agent_repository(agent_id, ctx):\n    return await _load_visible_agent(agent_id, ctx)\n"
)

Function = ast.FunctionDef | ast.AsyncFunctionDef


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Attribute):
        return f"{_dotted(node.value)}.{node.attr}"
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return ""


def _tail(node: ast.AST) -> str:
    return _dotted(node).split(".")[-1]


def _is_id_name(name: str) -> bool:
    return name == "id" or name.endswith("_id")


def _is_id_like(node: ast.expr) -> bool:
    if isinstance(node, ast.Attribute):
        return _is_id_name(node.attr)
    return isinstance(node, ast.Name) and _is_id_name(node.id)


def _arguments(call: ast.Call) -> list[ast.expr]:
    return [*call.args, *(keyword.value for keyword in call.keywords)]


def _route_decorator(fn: Function) -> ast.Call | None:
    for decorator in fn.decorator_list:
        if (
            isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Attribute)
            and decorator.func.attr in HTTP_VERBS
        ):
            return decorator
    return None


def _route_path(decorator: ast.Call) -> ast.expr | None:
    spelled = decorator.args[:1] + [k.value for k in decorator.keywords if k.arg == "path"]
    return spelled[0] if spelled else None


def _path_params(route: ast.expr | None) -> list[str] | None:
    if isinstance(route, ast.Constant) and isinstance(route.value, str):
        return PATH_PARAM_RE.findall(route.value)
    return None


def _parameters(fn: Function) -> list[ast.arg]:
    return fn.args.posonlyargs + fn.args.args + fn.args.kwonlyargs


def _tenant_ctx_names(fn: Function) -> list[str]:
    return [
        p.arg for p in _parameters(fn) if p.annotation is not None and _tail(p.annotation) == "TenantContext"
    ]


def _is_role_admin(node: ast.expr) -> bool:
    return isinstance(node, ast.Attribute) and node.attr == "ADMIN" and _tail(node.value) == "Role"


def _admin_gated(fn: Function) -> bool:
    roots = fn.decorator_list + [d for d in fn.args.defaults + fn.args.kw_defaults if d is not None]
    return any(
        isinstance(node, ast.Call)
        and _tail(node.func) == "require_role"
        and any(_is_role_admin(argument) for argument in _arguments(node))
        for root in roots
        for node in ast.walk(root)
    )


def _is_getter_call(node: ast.AST, getter: re.Pattern[str] | None) -> bool:
    return (
        isinstance(node, ast.Call)
        and not node.args
        and not node.keywords
        and (getter is None or getter.fullmatch(_tail(node.func)) is not None)
    )


def _bindings(fn: Function) -> list[tuple[ast.expr, list[str]]]:
    bound = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign):
            bound.append((node.value, [t.id for t in node.targets if isinstance(t, ast.Name)]))
        elif isinstance(node, ast.AnnAssign) and node.value is not None and isinstance(node.target, ast.Name):
            bound.append((node.value, [node.target.id]))
    return bound


def _receivers(fn: Function, getter: re.Pattern[str] | None = None) -> set[str]:
    return {name for value, names in _bindings(fn) if _is_getter_call(value, getter) for name in names}


def _on_service(call: ast.Call, receivers: set[str], getter: re.Pattern[str]) -> bool:
    if not isinstance(call.func, ast.Attribute):
        return False
    receiver = call.func.value
    if isinstance(receiver, ast.Name):
        return receiver.id in receivers
    return _is_getter_call(receiver, getter)


def _calls(fn: Function) -> list[ast.Call]:
    return [node for node in ast.walk(fn) if isinstance(node, ast.Call)]


def _own_scope(fn: Function) -> list[ast.AST]:
    nodes: list[ast.AST] = []
    todo: list[ast.AST] = list(fn.body)
    while todo:
        node = todo.pop()
        nodes.append(node)
        if not isinstance(node, NESTED_SCOPES):
            todo.extend(ast.iter_child_nodes(node))
    return nodes


def _own_calls(fn: Function) -> list[ast.Call]:
    nodes = _own_scope(fn)
    discarded = [
        n.value
        for n in nodes
        if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call) and _tail(n.value.func) == "visible"
    ]
    return [n for n in nodes if isinstance(n, ast.Call) and n not in discarded]


def _id_locals(fn: Function) -> dict[str, str]:
    return {name: _tail(value) for value, names in _bindings(fn) if _is_id_like(value) for name in names}


def _id_param(node: ast.expr, id_locals: dict[str, str]) -> str:
    if _is_id_like(node):
        return _tail(node)
    return id_locals.get(node.id, "") if isinstance(node, ast.Name) else ""


def _reads(fn: Function) -> list[tuple[ast.Call, str]]:
    receivers, id_locals = _receivers(fn), _id_locals(fn)
    reads = []
    for call in _calls(fn):
        params = [param for a in _arguments(call) if (param := _id_param(a, id_locals))]
        if params and _on_service(call, receivers, SERVICE_GETTER_RE):
            reads.append((call, params[0]))
    return reads


def _is_named_scoping(name: str) -> bool:
    return name in SCOPING_CALLS or name.startswith("_load_visible")


def _route_names(path: Path) -> dict[str, tuple[str, str]]:
    tree = ast.parse(path.read_text(), filename=str(path))
    names = {fn.name: (path.stem, fn.name) for fn in tree.body if isinstance(fn, Function)}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(f"{ROUTE_PACKAGE}."):
            module = (node.module or "").removeprefix(f"{ROUTE_PACKAGE}.")
            names.update({a.asname or a.name: (module, a.name) for a in node.names})
        elif isinstance(node, ast.Import):
            names.update(
                {
                    a.asname: (a.name.removeprefix(f"{ROUTE_PACKAGE}."), "")
                    for a in node.names
                    if a.asname and a.name.startswith(f"{ROUTE_PACKAGE}.")
                }
            )
    return names


def _is_scoping_call(
    call: ast.Call, bound: dict[str, tuple[str, str]], scoping: set[tuple[str, str]]
) -> bool:
    func = call.func
    if isinstance(func, ast.Name):
        return _is_named_scoping(func.id) or bound.get(func.id) in scoping
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        module = bound.get(func.value.id)
        return module is not None and module[1] == "" and (module[0], func.attr) in scoping
    return False


def _hands_on(
    fn: Function, ctx_names: set[str], bound: dict[str, tuple[str, str]], scoping: set[tuple[str, str]]
) -> bool:
    receivers = _receivers(fn, SERVICE_RE)
    return any(
        any(isinstance(a, ast.Name) and a.id in ctx_names for a in _arguments(call))
        and (_is_scoping_call(call, bound, scoping) or _on_service(call, receivers, SERVICE_RE))
        for call in _own_calls(fn)
    )


def _rebinds(fn: Function, ctx_names: set[str]) -> list[str]:
    declared = (node for node in ast.walk(fn) if isinstance(node, ast.Global | ast.Nonlocal))
    bound = {name for node in declared for name in node.names}
    for node in _own_scope(fn):
        if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
            bound.add(node.id)
        elif isinstance(node, ast.Import | ast.ImportFrom):
            bound.update((alias.asname or alias.name).partition(".")[0] for alias in node.names)
        elif isinstance(node, NAMED_BINDINGS) and node.name:
            bound.add(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            bound.add(node.rest)
    return sorted(bound & ctx_names)


def _aliases(fn: Function, names: set[str]) -> set[str]:
    return {
        alias
        for value, targets in _bindings(fn)
        if isinstance(value, ast.Name) and value.id in names
        for alias in targets
    }


def _functions(path: Path) -> list[Function]:
    tree = ast.parse(path.read_text(), filename=str(path))
    return [n for n in tree.body if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)]


def _route_modules(routes: Path) -> list[Path]:
    return sorted(p for p in routes.glob("*.py") if p.name != "__init__.py")


def scoping_functions(routes: Path = ROUTES) -> set[tuple[str, str]]:
    modules = [(path.stem, _route_names(path), _functions(path)) for path in _route_modules(routes)]
    scoping = {
        (stem, fn.name) for stem, _, functions in modules for fn in functions if _is_named_scoping(fn.name)
    }
    grew = True
    while grew:
        grew = False
        for stem, bound, functions in modules:
            for fn in functions:
                if (stem, fn.name) not in scoping and any(
                    _is_scoping_call(c, bound, scoping) for c in _own_calls(fn)
                ):
                    scoping.add((stem, fn.name))
                    grew = True
    return scoping


def route_handlers(routes: Path = ROUTES) -> list[Handler]:
    scoping = scoping_functions(routes)
    handlers = []
    for path in _route_modules(routes):
        bound = _route_names(path)
        for fn in _functions(path):
            decorator = _route_decorator(fn)
            if decorator is None:
                continue
            route = _route_path(decorator)
            tenant_ctx = _tenant_ctx_names(fn)
            aliases = _aliases(fn, set(tenant_ctx))
            handlers.append(
                Handler(
                    path.name,
                    fn.name,
                    fn.lineno,
                    "no path" if route is None else ast.unparse(route),
                    _path_params(route),
                    [p.arg for p in _parameters(fn) if _is_id_name(p.arg)],
                    _reads(fn),
                    tenant_ctx,
                    _admin_gated(fn),
                    _hands_on(fn, set(tenant_ctx), bound, scoping),
                    _rebinds(fn, set(tenant_ctx)),
                    sorted(alias for alias in aliases if _hands_on(fn, {alias}, bound, scoping)),
                )
            )
    return handlers


def _is_visibility_helper(name: str) -> bool:
    return name in VISIBILITY_HELPERS or name.startswith("_load_visible")


def visibility_helpers_defined(routes: Path = ROUTES) -> list[tuple[str, str]]:
    return [
        (path.name, fn.name)
        for path in _route_modules(routes)
        for fn in _functions(path)
        if _is_visibility_helper(fn.name)
    ]


def _helper_for(param: str, method: str = "get") -> str:
    if method in HELPER_FOR_METHOD:
        return HELPER_FOR_METHOD[method]
    if param in HELPER_FOR_ID:
        return HELPER_FOR_ID[param]
    if _is_id_name(param) and len(param) > 3:
        return f"_load_visible_{param[:-3]}"
    return "_load_visible_<resource>"


def _evidence(h: Handler) -> tuple[str, str, str]:
    if h.path_params:
        return f"takes {{{h.path_params[0]}}}", h.path_params[0], "get"
    if h.id_params:
        return f"takes the id parameter {h.id_params[0]}", h.id_params[0], "get"
    read, param = h.reads[0]
    return f"reads {ast.unparse(read)}", param, _tail(read.func)


def _advice(param: str, method: str, ctx: str) -> str:
    helper = _helper_for(param, method)
    return f"require_member({ctx}, …)" if helper == "require_member" else f"{helper}(…, {ctx})"


def unscoped_handlers(routes: Path = ROUTES) -> list[Finding]:
    findings = []
    for h in route_handlers(routes):
        key = (Path(h.file).stem, h.name)
        if h.file in TENANT_WIDE_MODULES or h.admin_gated:
            continue
        if h.path_params is None:
            findings.append(
                Finding(
                    key,
                    f"{h.file}:{h.name} spells its path as {h.route}, not a string literal: write the path "
                    "in the decorator so the census reads its parameters",
                )
            )
            continue
        if not (h.path_params or h.id_params or h.reads) or (h.scoped and not h.rebinds):
            continue
        evidence, param, method = _evidence(h)
        where = f"{h.file}:{h.name} {evidence}"
        if not h.tenant_ctx:
            message = (
                f"{where} and no TenantContext: declare `ctx: TenantContext = RBACDepends(get_tenant_ctx)` "
                f"and scope it through {_advice(param, method, 'ctx')}"
            )
        elif h.rebinds:
            ctx = h.rebinds[0]
            message = (
                f"{where} and rebinds {ctx}: pass the declared {ctx} itself, {_advice(param, method, ctx)}"
            )
        elif h.aliases:
            ctx = h.tenant_ctx[0]
            message = (
                f"{where} and hands {ctx} to a scoping call only as {h.aliases[0]}: pass {ctx} itself, "
                f"{_advice(param, method, ctx)}"
            )
        else:
            ctx = h.tenant_ctx[0]
            message = (
                f"{where} and hands {ctx} to no scoping call: scope it through "
                f"{_advice(param, method, ctx)} or hand {ctx} to the service call"
            )
        findings.append(Finding(key, message))
    return findings


def _served_endpoints(routes: list[BaseRoute]) -> set[tuple[str, str]]:
    served = set()
    for route in routes:
        if isinstance(route, _IncludedRouter):
            served |= _served_endpoints(route.original_router.routes)
        elif isinstance(route, APIRoute):
            served.add((route.endpoint.__module__, route.endpoint.__name__))
    return served


def test_every_id_bearing_handler_hands_its_tenant_context_to_a_scoping_call():
    findings = [f for f in unscoped_handlers() if f.key not in BY_DESIGN_PRINCIPAL_SCOPED_HANDLERS]
    assert findings == [], "\n".join(f.message for f in findings)


def test_the_by_design_handlers_are_exactly_the_findings_they_excuse():
    found = {f.key for f in unscoped_handlers()}
    stale = sorted(set(BY_DESIGN_PRINCIPAL_SCOPED_HANDLERS) - found)
    assert stale == [], f"no longer a census finding, so remove it from the by-design handlers: {stale}"
    unexplained = sorted(k for k, reason in BY_DESIGN_PRINCIPAL_SCOPED_HANDLERS.items() if not reason.strip())
    assert unexplained == [], f"a by-design handler carries its reason: {unexplained}"


def test_the_census_reads_every_endpoint_the_app_serves():
    import main

    served = _served_endpoints(main.app.routes)
    assert served >= APP_LEVEL_ENDPOINTS, sorted(APP_LEVEL_ENDPOINTS - served)
    served -= APP_LEVEL_ENDPOINTS
    read = {(f"api.routes.{Path(h.file).stem}", h.name) for h in route_handlers()}
    assert len(read) >= ROUTE_HANDLER_FLOOR, len(read)
    assert sorted(served - read) == [], (
        "the app serves these endpoints and the census never reads them; register them with a "
        "@router.<verb>(...) decorator on a module-level function under src/api/routes/"
    )
    assert sorted(read - served) == [], "the census reads these handlers and the app never serves them"


def test_tenant_wide_modules_exist_and_carry_a_reason():
    missing = sorted(name for name in TENANT_WIDE_MODULES if not (ROUTES / name).is_file())
    assert missing == [], f"not a route module under {ROUTES}: {missing}"
    unexplained = sorted(name for name, reason in TENANT_WIDE_MODULES.items() if not reason.strip())
    assert unexplained == [], f"a tenant-wide module carries its reason: {unexplained}"
    admin_only = {name for name, reason in TENANT_WIDE_MODULES.items() if ADMIN_ONLY_CLAIM in reason}
    assert {"tenants.py", "users_admin.py"} <= admin_only, sorted(admin_only)
    not_gated = sorted(
        f"{h.file}:{h.name}" for h in route_handlers() if h.file in admin_only and not h.admin_gated
    )
    assert not_gated == [], (
        f"the module's reason claims {ADMIN_ONLY_CLAIM!r}, this handler is not: {not_gated}; gate it "
        "`_=RBACDepends(require_role(Role.ADMIN))` or take the module out of TENANT_WIDE_MODULES"
    )


def test_the_census_has_something_to_scan():
    modules = _route_modules(ROUTES)
    handlers = route_handlers()
    helpers = visibility_helpers_defined()
    assert len(modules) >= ROUTE_MODULE_FLOOR, sorted(p.name for p in modules)
    assert len(handlers) >= ROUTE_HANDLER_FLOOR, len(handlers)
    assert len(helpers) >= VISIBILITY_HELPER_FLOOR, helpers


def _fixture_findings() -> dict[str, str]:
    return {f.key[1]: f.message for f in unscoped_handlers(FIXTURES)}


def _fixture_handler(name: str) -> Handler:
    return next(h for h in route_handlers(FIXTURES) if h.name == name)


def test_fixture_carries_every_case_by_name():
    tree = ast.parse((FIXTURES / "routes_fixture.py").read_text())
    defined = {n.name for n in tree.body if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)}
    assert FIXTURE_HANDLERS <= defined, sorted(FIXTURE_HANDLERS - defined)
    handlers = {h.name for h in route_handlers(FIXTURES)}
    assert handlers == FIXTURE_HANDLERS - FIXTURE_HELPERS, sorted(handlers)
    helpers = {("routes_fixture", name) for name in FIXTURE_HELPERS}
    assert helpers <= scoping_functions(FIXTURES), sorted(helpers)


def test_fixture_a_scoped_handler_passes():
    assert "get_widget" not in _fixture_findings()
    assert _fixture_handler("get_widget").scoped


def test_fixture_a_handler_without_a_tenant_context_fails():
    assert _fixture_findings()["get_widget_unscoped"] == (
        "routes_fixture.py:get_widget_unscoped takes {widget_id} and no TenantContext: declare "
        "`ctx: TenantContext = RBACDepends(get_tenant_ctx)` and scope it through "
        "_load_visible_widget(…, ctx)"
    )


def test_fixture_an_admin_gated_handler_passes():
    assert "archive_widget" not in _fixture_findings()
    handler = _fixture_handler("archive_widget")
    assert handler.admin_gated and not handler.tenant_ctx and handler.path_params == ["widget_id"]


def test_fixture_an_admin_route_over_the_tenant_itself_passes():
    assert "get_tenant_admin" not in _fixture_findings()
    assert _fixture_handler("get_tenant_admin").admin_gated


def test_fixture_a_child_handler_calling_the_parents_helper_passes():
    assert "get_widget_part" not in _fixture_findings()
    assert _fixture_handler("get_widget_part").scoped


def test_fixture_a_tenant_context_the_handler_never_hands_on_fails():
    assert _fixture_findings()["get_gadget_raw"] == (
        "routes_fixture.py:get_gadget_raw takes {id} and hands ctx to no scoping call: scope it through "
        "_load_visible_<resource>(…, ctx) or hand ctx to the service call"
    )


def test_fixture_two_step_read_with_ctx_fails():
    assert _fixture_findings()["two_step_read_with_ctx"] == (
        "routes_fixture.py:two_step_read_with_ctx takes {widget_id} and hands ctx to no scoping call: "
        "scope it through _load_visible_widget(…, ctx) or hand ctx to the service call"
    )


def test_fixture_query_id_without_ctx_fails():
    assert _fixture_findings()["query_id_without_ctx"] == (
        "routes_fixture.py:query_id_without_ctx takes the id parameter widget_id and no TenantContext: "
        "declare `ctx: TenantContext = RBACDepends(get_tenant_ctx)` and scope it through "
        "_load_visible_widget(…, ctx)"
    )


def test_fixture_body_id_without_ctx_fails():
    assert _fixture_findings()["body_id_without_ctx"] == (
        "routes_fixture.py:body_id_without_ctx reads get_widget_service().get(req.widget_id) and no "
        "TenantContext: declare `ctx: TenantContext = RBACDepends(get_tenant_ctx)` and scope it through "
        "_load_visible_widget(…, ctx)"
    )


def test_fixture_ctx_only_logged_fails():
    assert _fixture_findings()["ctx_only_logged"] == (
        "routes_fixture.py:ctx_only_logged takes {widget_id} and hands ctx to no scoping call: scope it "
        "through _load_visible_widget(…, ctx) or hand ctx to the service call"
    )


def test_fixture_path_keyword_without_ctx_fails():
    assert _fixture_findings()["path_keyword_without_ctx"] == (
        "routes_fixture.py:path_keyword_without_ctx takes {widget_id} and no TenantContext: declare "
        "`ctx: TenantContext = RBACDepends(get_tenant_ctx)` and scope it through "
        "_load_visible_widget(…, ctx)"
    )


def test_fixture_grants_shape_two_step_fails():
    assert _fixture_findings()["grants_shape_two_step"] == (
        "routes_fixture.py:grants_shape_two_step takes {agent_id} and hands ctx to no scoping call: "
        "scope it through _load_visible_agent(…, ctx) or hand ctx to the service call"
    )


def test_fixture_aliased_ctx_is_flagged():
    assert _fixture_findings()["aliased_ctx"] == (
        "routes_fixture.py:aliased_ctx takes {widget_id} and hands ctx to a scoping call only as scope: "
        "pass ctx itself, _load_visible_widget(…, ctx)"
    )


def test_fixture_create_with_require_member_passes():
    assert "create_with_require_member" not in _fixture_findings()
    handler = _fixture_handler("create_with_require_member")
    assert handler.reads and not handler.path_params and not handler.id_params and handler.scoped


def test_fixture_admin_name_in_a_query_default_fails():
    assert _fixture_findings()["admin_name_in_a_query_default"] == (
        "routes_fixture.py:admin_name_in_a_query_default takes {widget_id} and no TenantContext: declare "
        "`ctx: TenantContext = RBACDepends(get_tenant_ctx)` and scope it through "
        "_load_visible_widget(…, ctx)"
    )


def test_fixture_forged_global_ctx_fails():
    assert _fixture_findings()["forged_global_ctx"] == (
        "routes_fixture.py:forged_global_ctx takes {widget_id} and hands ctx to no scoping call: scope it "
        "through _load_visible_widget(…, ctx) or hand ctx to the service call"
    )


def test_fixture_annotated_receiver_body_id_fails():
    assert _fixture_findings()["annotated_receiver_body_id"] == (
        "routes_fixture.py:annotated_receiver_body_id reads svc.get(req.widget_id) and no TenantContext: "
        "declare `ctx: TenantContext = RBACDepends(get_tenant_ctx)` and scope it through "
        "_load_visible_widget(…, ctx)"
    )


def _no_scoping_call(name: str) -> str:
    return (
        f"routes_fixture.py:{name} takes {{widget_id}} and hands ctx to no scoping call: scope it through "
        "_load_visible_widget(…, ctx) or hand ctx to the service call"
    )


def _findings_beside_a_scoping_agents_module(routes: Path) -> dict[str, str]:
    shutil.copy(FIXTURES / "routes_fixture.py", routes)
    (routes / "agents.py").write_text(SCOPING_AGENTS_MODULE)
    assert {("agents", "_gate"), ("agents", "_agent_repository")} <= scoping_functions(routes)
    return {f.key[1]: f.message for f in unscoped_handlers(routes)}


def test_fixture_gate_scoping_only_elsewhere_fails_beside_another_modules_scoping_gate(tmp_path):
    findings = _findings_beside_a_scoping_agents_module(tmp_path)
    assert findings["gate_scoping_only_elsewhere"] == _no_scoping_call("gate_scoping_only_elsewhere")


def test_fixture_an_imported_scoping_function_scopes_through_its_module(tmp_path):
    name = "repository_through_an_imported_scoping_function"
    assert name not in _findings_beside_a_scoping_agents_module(tmp_path)
    assert _fixture_findings()[name] == (
        f"routes_fixture.py:{name} takes {{agent_id}} and hands ctx to no scoping call: scope it through "
        "_load_visible_agent(…, ctx) or hand ctx to the service call"
    )


def test_fixture_body_id_read_in_a_lambda_fails():
    assert _fixture_findings()["body_id_read_in_a_lambda"] == (
        "routes_fixture.py:body_id_read_in_a_lambda reads get_widget_service().get(r.widget_id) and no "
        "TenantContext: declare `ctx: TenantContext = RBACDepends(get_tenant_ctx)` and scope it through "
        "_load_visible_widget(…, ctx)"
    )


def test_fixture_rebound_ctx_fails():
    assert _fixture_findings()["rebound_ctx"] == (
        "routes_fixture.py:rebound_ctx takes {widget_id} and rebinds ctx: pass the declared ctx itself, "
        "_load_visible_widget(…, ctx)"
    )


def test_fixture_ctx_rebound_by_an_import_fails():
    assert _fixture_findings()["ctx_rebound_by_an_import"] == (
        "routes_fixture.py:ctx_rebound_by_an_import takes {widget_id} and rebinds ctx: pass the declared "
        "ctx itself, _load_visible_widget(…, ctx)"
    )


def test_fixture_ctx_rebound_by_except_as_fails():
    assert _fixture_findings()["ctx_rebound_by_except_as"] == (
        "routes_fixture.py:ctx_rebound_by_except_as takes {widget_id} and rebinds ctx: pass the declared "
        "ctx itself, _load_visible_widget(…, ctx)"
    )


def test_fixture_ctx_logged_through_a_logger_getter_fails():
    name = "ctx_logged_through_a_logger_getter"
    assert _fixture_findings()[name] == _no_scoping_call(name)


def test_fixture_visible_on_a_non_module_fails():
    assert _fixture_findings()["visible_on_a_non_module"] == _no_scoping_call("visible_on_a_non_module")


def test_fixture_visible_result_discarded_fails():
    assert _fixture_findings()["visible_result_discarded"] == _no_scoping_call("visible_result_discarded")


def test_fixture_scoping_only_in_nested_scopes_fails():
    name = "scoping_only_in_nested_scopes"
    assert _fixture_findings()[name] == _no_scoping_call(name)


def test_fixture_body_id_through_a_local_fails():
    assert _fixture_findings()["body_id_through_a_local"] == (
        "routes_fixture.py:body_id_through_a_local reads get_widget_service().get(target) and no "
        "TenantContext: declare `ctx: TenantContext = RBACDepends(get_tenant_ctx)` and scope it through "
        "_load_visible_widget(…, ctx)"
    )


def test_fixture_path_in_a_constant_fails():
    assert _fixture_findings()["path_in_a_constant"] == (
        "routes_fixture.py:path_in_a_constant spells its path as WIDGET_BY_NAME, not a string literal: "
        "write the path in the decorator so the census reads its parameters"
    )
