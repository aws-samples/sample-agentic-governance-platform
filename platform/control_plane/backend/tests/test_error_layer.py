"""The error layer: `core.errors.AppError`, the one handler `main.py` installs, and the rules judged over
every module under `src`: no `HTTPException` outside the auth dependencies in
`HTTP_EXCEPTION_MODULES`, no translation in a route, and every raised kind in the table of the class
its raise site names. A table belongs to one class, keyed by `(module, qualname)`, so two classes
that share a name keep two tables.
"""

from __future__ import annotations

import ast
import functools
import importlib
import json
import logging
import subprocess
import sys
import textwrap
import types
from collections.abc import Callable, Collection, Mapping
from pathlib import Path
from typing import ClassVar, NamedTuple

import pytest
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException as StarletteHTTPException

import core.errors
from core.errors import MESSAGE, AppError, Outcome
from core.registry_resolver import (
    AmbiguousRegistryNameError,
    RegistryNotConfiguredError,
    RegistryNotFoundError,
)
from main import app
from models.errors import ErrorBody

SRC = Path(__file__).resolve().parents[1] / "src"
ROUTES = SRC / "api" / "routes"
HTTP_EXCEPTION = "HTTPException"
HTTP_EXCEPTION_MODULES: frozenset[str] = frozenset(
    {"core/security_entra.py", "core/security_github_oidc.py", "core/rbac.py"}
)

ClassKey = tuple[str, str]

TRANSLATION = (
    "re-parent the service error to core.errors.AppError and delete this block, "
    "or log it with logger.exception"
)
INDIRECT_TRANSLATION = (
    "translates through {helper}(); re-parent the service error to core.errors.AppError and delete "
    "this block and the helper, or log it with logger.exception"
)
APP_ERROR_TRANSLATION = (
    "raises an AppError kind inside except{through}; raise it in the service that decides the failure "
    "and delete this block"
)
HTTP_EXCEPTION_FRAMEWORKS = ("fastapi", "starlette")
HTTP_EXCEPTION_REMEDY = (
    "raise the AppError kind of the domain that decides the refusal (a core.errors.AppError subclass in "
    "its service); only the auth dependencies in HTTP_EXCEPTION_MODULES answer 401 with HTTPException"
)
HTTP_EXCEPTION_USE = f"uses HTTPException; {HTTP_EXCEPTION_REMEDY}"
HTTP_EXCEPTION_REACH = "reaches HTTPException through {how}; " + HTTP_EXCEPTION_REMEDY
HTTP_EXCEPTION_UNRESOLVED = (
    "calls getattr on {module} with a name that is not a literal, which may be HTTPException; "
    "name the attribute literally"
)
HTTP_EXCEPTION_REBOUND = (
    "binds HTTPException to a second name; an auth dependency binds it by its own import alone, so no "
    "module outside HTTP_EXCEPTION_MODULES reaches it through this one"
)
HTTP_EXCEPTION_HANDED = (
    "uses HTTPException other than to call, raise or catch it; an auth dependency hands it to no other "
    "module, so none outside HTTP_EXCEPTION_MODULES reaches it through this one"
)
STALE_HTTP_EXCEPTION_MODULE = "{module} no longer uses HTTPException; delete it from HTTP_EXCEPTION_MODULES"
DOCUMENT_CODES = textwrap.dedent(
    """
    import json
    from main import app
    code = app.openapi()["components"]["schemas"]["ErrorBody"]["properties"]["code"]
    print(json.dumps(code["enum"]))
    """
)


class _ScratchError(AppError):
    domain = "scratch"
    kinds: ClassVar = {
        "gone": Outcome(410, "it is gone"),
        "echo": Outcome(400, None),
        "shaped": Outcome(422, {"message": MESSAGE, "errors": []}),
        "down": Outcome(503, None),
    }


class _OneRowError(AppError):
    domain = "scratch"
    kinds: ClassVar = {"only": Outcome(409, None)}


def test_app_error_body_has_detail_and_code():
    err = _ScratchError("boom", "gone")

    assert err.status_code == 410
    assert err.body() == {"detail": "it is gone", "code": "scratch.gone"}
    assert (str(err), err.message, err.kind, err.extra) == ("boom", "boom", "gone", {})


def test_app_error_none_detail_uses_the_message():
    err = _ScratchError("the message", "echo")

    assert err.detail == "the message"
    assert err.body() == {"detail": "the message", "code": "scratch.echo"}


def test_app_error_dict_template_renders_message_and_merges_extra():
    err = _ScratchError("<msg>", "shaped", extra={"errors": ["a"]})

    assert err.detail == {"message": "<msg>", "errors": ["a"]}
    body = err.body()
    assert body == {"detail": {"message": "<msg>", "errors": ["a"]}, "code": "scratch.shaped"}
    assert "errors" not in body
    assert _ScratchError("<msg>", "shaped").detail == {"message": "<msg>", "errors": []}


def test_app_error_rendered_detail_is_a_fresh_copy():
    first = _ScratchError("one", "shaped", extra={"errors": ["a"]})

    assert isinstance(first.detail, dict)
    first.detail["errors"].append("mutated")

    assert _ScratchError.kinds["shaped"].detail == {"message": MESSAGE, "errors": []}
    assert first.extra == {"errors": ["a"]}
    assert first.detail == {"message": "one", "errors": ["a"]}
    assert _ScratchError("two", "shaped").detail == {"message": "two", "errors": []}


def test_app_error_string_detail_with_extra_is_a_type_error():
    with pytest.raises(TypeError, match="gone"):
        _ScratchError("boom", "gone", extra={"errors": []})
    with pytest.raises(TypeError, match="echo"):
        _ScratchError("boom", "echo", extra={"x": 1})
    assert _ScratchError("boom", "gone", extra={}).extra == {}


def test_app_error_single_row_table_needs_no_kind():
    err = _OneRowError("taken")

    assert (err.kind, err.status_code, err.code, err.detail) == ("only", 409, "scratch.only", "taken")
    assert _OneRowError("taken", "only").kind == "only"


def test_app_error_unknown_kind_is_a_type_error_naming_the_table():
    with pytest.raises(
        TypeError,
        match=r"^_ScratchError: unknown kind 'nope'; the table is \['down', 'echo', 'gone', 'shaped'\]$",
    ):
        _ScratchError("boom", "nope")
    with pytest.raises(TypeError, match=r"^_OneRowError: unknown kind 'other'; the table is \['only'\]$"):
        _OneRowError("boom", "other")


def test_app_error_none_kind_with_many_rows_is_a_type_error():
    with pytest.raises(TypeError, match=r"^_ScratchError: unknown kind None; the table is "):
        _ScratchError("boom")


@pytest.fixture
def scratch_client():
    import main

    async def raise_kind(kind: str):
        raise _ScratchError("the message", kind)

    async def needs_an_int(n: int):
        return {"n": n}

    before = list(main.app.router.routes)
    main.app.add_api_route("/__error_layer__/{kind}", raise_kind)
    main.app.add_api_route("/__error_layer_validation__", needs_an_int)
    try:
        yield TestClient(main.app, raise_server_exceptions=False), main
    finally:
        main.app.router.routes[:] = before


def test_handler_returns_status_and_body(scratch_client):
    client, _ = scratch_client

    resp = client.get("/__error_layer__/gone")

    assert resp.status_code == 410
    assert resp.json() == {"detail": "it is gone", "code": "scratch.gone"}


def test_handler_echoes_an_allowed_cors_origin(scratch_client):
    client, main = scratch_client
    origin = main.settings.CORS_ORIGINS[0]

    resp = client.get("/__error_layer__/gone", headers={"origin": origin})

    assert resp.status_code == 410
    assert resp.headers["access-control-allow-origin"] == origin
    assert resp.headers["access-control-allow-credentials"] == "true"


def test_handler_logs_5xx_with_exception_and_4xx_at_warning(scratch_client, caplog):
    client, _ = scratch_client

    with caplog.at_level(logging.WARNING, logger="core.errors"):
        assert client.get("/__error_layer__/down").status_code == 503
        assert client.get("/__error_layer__/gone").status_code == 410

    five, four = [r for r in caplog.records if r.name == "core.errors"]
    assert (five.levelno, five.getMessage()) == (logging.ERROR, "scratch.down")
    assert five.exc_info is not None and five.exc_info[0] is _ScratchError
    assert (four.levelno, four.getMessage()) == (logging.WARNING, "scratch.gone")
    assert four.exc_info is None


def test_handler_keeps_a_4xx_message_out_of_the_log(scratch_client, caplog):
    client, main = scratch_client
    token = "ghp_" + "x" * 36

    async def raise_echo():
        raise _ScratchError(token, "echo")

    main.app.add_api_route("/__error_layer_echo__", raise_echo)
    with caplog.at_level(logging.DEBUG):
        resp = client.get("/__error_layer_echo__")

    assert resp.status_code == 400
    assert resp.json() == {"detail": token, "code": "scratch.echo"}
    assert caplog.records
    assert all(token not in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize(
    "exc_type", [RegistryNotFoundError, RegistryNotConfiguredError, AmbiguousRegistryNameError]
)
def test_registry_resolution_errors_are_503_with_the_remedy(exc_type):
    exc = exc_type("No AWS Agent Registry named 'agp-agents'; run terraform apply")

    assert exc.status_code == 503
    assert exc.detail == str(exc) == "No AWS Agent Registry named 'agp-agents'; run terraform apply"
    assert exc.code.startswith("registry.")
    assert exc.body() == {"detail": str(exc), "code": exc.code}


def test_validation_errors_keep_fastapi_shape(scratch_client):
    client, _ = scratch_client

    resp = client.get("/__error_layer_validation__", params={"n": "abc"})

    assert resp.status_code == 422
    body = resp.json()
    assert isinstance(body["detail"], list)
    assert body["detail"][0]["loc"] == ["query", "n"]
    assert "code" not in body


def _is_named(node: ast.expr | None, name: str) -> bool:
    if isinstance(node, ast.Name):
        return node.id == name
    return isinstance(node, ast.Attribute) and node.attr == name


def _shown(path: Path) -> str:
    return path.relative_to(SRC).as_posix() if path.is_relative_to(SRC) else path.as_posix()


def _module_name(path: Path, root: Path) -> str:
    parts = path.relative_to(root).with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _dotted(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        value = _dotted(node.value)
        return None if value is None else f"{value}.{node.attr}"
    return None


def _origin(module: str, name: str) -> ClassKey:
    try:
        value = getattr(importlib.import_module(module), name, None)
    except ImportError:
        return (module, name)
    return (value.__module__, value.__qualname__) if isinstance(value, type) else (module, name)


class _Bindings(NamedTuple):
    classes: dict[str, set[ClassKey]]
    modules: dict[str, set[str]]

    def resolve(self, node: ast.expr) -> set[ClassKey]:
        if isinstance(node, ast.Name):
            return self.classes.get(node.id, set())
        if not isinstance(node, ast.Attribute):
            return set()
        dotted = _dotted(node.value)
        if dotted is None:
            return set()
        head, _, rest = dotted.partition(".")
        return {
            _origin(f"{bound}.{rest}" if rest else bound, node.attr)
            for bound in self.modules.get(head, set())
        }


def _class_resolver(tree: ast.Module, module: str) -> Callable[[ast.expr], set[ClassKey]]:
    """How a module names a class, as `(module, qualname)` candidates: `N` is the module's own
    top-level class `N` or the `Y` of a `from X import Y as N`; `m.Y` and `a.b.Y` are `Y` in the
    module that `import X as m`, `import a.b` or `from P import m` bound. An imported `Y` that is a
    class in its live module resolves to where that class is defined, so a re-export names its
    origin; an import that does not resolve to a class stays `(X, Y)`."""
    bindings = _Bindings({}, {})
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            bindings.classes.setdefault(node.name, set()).add((module, node.name))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                local = alias.asname or alias.name
                bindings.classes.setdefault(local, set()).add(_origin(node.module, alias.name))
                bindings.modules.setdefault(local, set()).add(f"{node.module}.{alias.name}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name.partition(".")[0]
                bindings.modules.setdefault(local, set()).add(alias.name if alias.asname else local)
    return bindings.resolve


def _import_every_src_module() -> None:
    for path in sorted(SRC.rglob("*.py")):
        if path != SRC / "__init__.py":
            importlib.import_module(_module_name(path, SRC))


def _defined_under_src(cls: type) -> bool:
    path = SRC.joinpath(*cls.__module__.split("."))
    return path != SRC and (path.with_suffix(".py").is_file() or (path / "__init__.py").is_file())


def _src_subclasses() -> list[type[AppError]]:
    found: list[type[AppError]] = []
    pending = list(AppError.__subclasses__())
    while pending:
        cls = pending.pop()
        pending.extend(cls.__subclasses__())
        if _defined_under_src(cls):
            found.append(cls)
    return sorted(found, key=lambda cls: (cls.__module__, cls.__qualname__))


@functools.cache
def _app_error_classes() -> dict[ClassKey, type[AppError]]:
    _import_every_src_module()
    return {(cls.__module__, cls.__qualname__): cls for cls in _src_subclasses()}


def _refusal_raised(
    node: ast.AST, resolve: Callable[[ast.expr], set[ClassKey]], app_errors: set[ClassKey]
) -> str | None:
    for child in ast.walk(node):
        if not (isinstance(child, ast.Raise) and child.exc is not None):
            continue
        raised = child.exc.func if isinstance(child.exc, ast.Call) else child.exc
        if _is_named(raised, HTTP_EXCEPTION):
            return HTTP_EXCEPTION
        if resolve(raised) & app_errors:
            return AppError.__name__
    return None


def _translation(raised: str, helper: str | None) -> str:
    if raised == HTTP_EXCEPTION:
        return TRANSLATION if helper is None else INDIRECT_TRANSLATION.format(helper=helper)
    return APP_ERROR_TRANSLATION.format(through="" if helper is None else f" through {helper}()")


def _catches_exception(handler: ast.ExceptHandler) -> bool:
    caught = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return any(_is_named(node, "Exception") for node in caught)


def _logs_or_reraises(handler: ast.ExceptHandler) -> bool:
    for stmt in handler.body:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Raise):
                return True
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr == "exception":
                return True
            if node.func.attr == "error" and any(
                kw.arg == "exc_info" and isinstance(kw.value, ast.Constant) and kw.value.value is True
                for kw in node.keywords
            ):
                return True
    return False


def _helper_called(handler: ast.ExceptHandler, helpers: Collection[str]) -> str | None:
    """The same-module raise helper the handler calls as a statement or raises the value of, if any."""
    for stmt in handler.body:
        for node in ast.walk(stmt):
            call = (
                node.value
                if isinstance(node, ast.Expr)
                else node.exc
                if isinstance(node, ast.Raise)
                else None
            )
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id in helpers:
                return call.func.id
    return None


def translating_handlers(path: Path, root: Path) -> list[str]:
    """Every `except` in `path` that raises `HTTPException` or an `AppError` subclass, directly or
    through one same-module helper that does, or catches `Exception` without logging through
    `logger.exception` / `logger.error(..., exc_info=True)` or re-raising. A raised class is an
    `AppError` subclass when `_class_resolver` names it one of `src`'s."""
    tree = ast.parse(path.read_text())
    resolve = _class_resolver(tree, _module_name(path, root))
    app_errors = set(_app_error_classes())
    raised_by = {
        node.name: _refusal_raised(node, resolve, app_errors)
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    helpers = {name: raised for name, raised in raised_by.items() if raised is not None}
    findings = []
    for handler in ast.walk(tree):
        if not isinstance(handler, ast.ExceptHandler):
            continue
        where = f"{_shown(path)}:{handler.lineno}"
        raised = _refusal_raised(handler, resolve, app_errors)
        helper = _helper_called(handler, helpers)
        if raised is not None:
            findings.append(f"{where}: {_translation(raised, None)}")
        elif helper is not None:
            findings.append(f"{where}: {_translation(helpers[helper], helper)}")
        elif _catches_exception(handler) and not _logs_or_reraises(handler):
            findings.append(f"{where}: {TRANSLATION}")
    return findings


def test_routes_do_not_translate_service_errors():
    files = sorted(path for path in ROUTES.glob("*.py") if path.name != "__init__.py")

    findings = [line for path in files for line in translating_handlers(path, SRC)]

    assert findings == [], "\n".join(findings)


def test_route_rule_follows_same_module_raise_helpers(tmp_path):
    module = tmp_path / "scratch_route.py"
    module.write_text(
        textwrap.dedent(
            """
            from fastapi import HTTPException


            def _raise_it(err):
                raise HTTPException(status_code=409, detail=str(err))


            def _log_it(err):
                logger.warning("%s", err)


            def route():
                try:
                    work()
                except ValueError as err:
                    _raise_it(err)
                try:
                    work()
                except ValueError as err:
                    raise _raise_it(err)
                try:
                    work()
                except ValueError as err:
                    _log_it(err)
                try:
                    work()
                except ValueError:
                    raise HTTPException(status_code=400, detail="direct")
            """
        )
    )

    findings = translating_handlers(module, tmp_path)

    through_helper = INDIRECT_TRANSLATION.format(helper="_raise_it")
    assert findings == [
        f"{module.as_posix()}:16: {through_helper}",
        f"{module.as_posix()}:20: {through_helper}",
        f"{module.as_posix()}:28: {TRANSLATION}",
    ]


def test_route_rule_flags_an_app_error_raised_inside_an_except(tmp_path):
    module = tmp_path / "scratch_route.py"
    module.write_text(
        textwrap.dedent(
            """
            from services.tenant_service import TenantError


            def _refuse(err):
                raise TenantError(str(err), kind="not_permitted")


            def route():
                try:
                    work()
                except ValueError as err:
                    raise TenantError(str(err), kind="not_permitted") from err
                try:
                    work()
                except Exception:
                    logger.exception("discovery failed")
                    raise TenantError("discovery failed", kind="discovery_failed")
                try:
                    work()
                except ValueError as err:
                    _refuse(err)
                try:
                    work()
                except TenantError:
                    raise
                try:
                    work()
                except ValueError as err:
                    raise RuntimeError(str(err)) from err
            """
        )
    )

    findings = translating_handlers(module, tmp_path)

    direct = APP_ERROR_TRANSLATION.format(through="")
    assert findings == [
        f"{module.as_posix()}:12: {direct}",
        f"{module.as_posix()}:16: {direct}",
        f"{module.as_posix()}:21: {APP_ERROR_TRANSLATION.format(through=' through _refuse()')}",
    ]


def http_exception_uses(path: Path) -> list[str]:
    """Every line of `path` that imports `HTTPException`, under its name or an alias, or names it by
    that name, an alias or an attribute (`fastapi.HTTPException`): a raise, a base, an `except` or
    any other reference."""
    tree = ast.parse(path.read_text())
    imports = [
        (node.lineno, alias.asname)
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
        if alias.name == HTTP_EXCEPTION
    ]
    names = {HTTP_EXCEPTION} | {asname for _, asname in imports if asname}
    lines = {line for line, _ in imports} | {
        node.lineno
        for node in ast.walk(tree)
        if (isinstance(node, ast.Name) and node.id in names)
        or (isinstance(node, ast.Attribute) and node.attr == HTTP_EXCEPTION)
    }
    return [f"{_shown(path)}:{line}: {HTTP_EXCEPTION_USE}" for line in sorted(lines)]


def test_no_module_imports_raises_subclasses_or_catches_http_exception():
    uses = {_shown(path): http_exception_uses(path) for path in sorted(SRC.rglob("*.py"))}

    findings = [
        line for module, lines in uses.items() if module not in HTTP_EXCEPTION_MODULES for line in lines
    ]
    stale = [
        STALE_HTTP_EXCEPTION_MODULE.format(module=module)
        for module in sorted(HTTP_EXCEPTION_MODULES)
        if not uses.get(module)
    ]
    assert findings == [], "\n".join(findings)
    assert stale == [], "\n".join(stale)


def test_the_http_exception_zero_target_catches_an_aliased_import(tmp_path):
    module = tmp_path / "scratch_route.py"
    module.write_text(
        textwrap.dedent(
            """
            from fastapi import HTTPException as HE


            def route():
                raise HE(404)
            """
        )
    )

    assert http_exception_uses(module) == [
        f"{module.as_posix()}:2: {HTTP_EXCEPTION_USE}",
        f"{module.as_posix()}:6: {HTTP_EXCEPTION_USE}",
    ]


def test_the_http_exception_zero_target_names_every_shape(tmp_path):
    module = tmp_path / "scratch_route.py"
    module.write_text(
        textwrap.dedent(
            """
            import fastapi
            from starlette.exceptions import HTTPException


            class Gone(HTTPException):
                pass


            def route():
                try:
                    raise fastapi.HTTPException(status_code=404)
                except (ValueError, HTTPException) as err:
                    return isinstance(err, HTTPException)
            """
        )
    )
    clean = tmp_path / "clean_route.py"
    clean.write_text("from core.errors import AppError\n\n\ndef route():\n    raise AppError('m')\n")

    assert http_exception_uses(module) == [
        f"{module.as_posix()}:{line}: {HTTP_EXCEPTION_USE}" for line in (3, 6, 12, 13, 14)
    ]
    assert http_exception_uses(clean) == []


def _live(module: str, name: str | None = None) -> object:
    try:
        imported = importlib.import_module(module)
    except ImportError:
        return None
    return imported if name is None else getattr(imported, name, None)


def _is_http_exception(value: object) -> bool:
    return isinstance(value, type) and issubclass(value, StarletteHTTPException)


def _import_binding(alias: ast.alias) -> tuple[str, object]:
    module = _live(alias.name)
    head = alias.name.partition(".")[0]
    return (alias.asname, module) if alias.asname else (head, _live(head))


def _module_bindings(tree: ast.Module) -> dict[str, list[types.ModuleType]]:
    bound: dict[str, list[types.ModuleType]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            pairs = [_import_binding(alias) for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            pairs = [(alias.asname or alias.name, _live(node.module, alias.name)) for alias in node.names]
        else:
            continue
        for local, value in pairs:
            if isinstance(value, types.ModuleType):
                bound.setdefault(local, []).append(value)
    return bound


def _resolved(node: ast.expr, bound: Mapping[str, list[types.ModuleType]]) -> list[object]:
    head, *rest = (_dotted(node) or "").split(".")
    values: list[object] = []
    for module in bound.get(head, []):
        value: object = module
        for attr in rest:
            value = getattr(value, attr, None) if isinstance(value, (types.ModuleType, type)) else None
        values.append(value)
    return values


def _getattr_frameworks(node: ast.Call, bound: Mapping[str, list[types.ModuleType]]) -> list[str]:
    if not (_is_named(node.func, "getattr") and len(node.args) >= 2):
        return []
    name = node.args[1]
    if isinstance(name, ast.Constant) and isinstance(name.value, str):
        return []
    return sorted(
        {
            value.__name__
            for value in _resolved(node.args[0], bound)
            if isinstance(value, types.ModuleType)
            and value.__name__.partition(".")[0] in HTTP_EXCEPTION_FRAMEWORKS
        }
    )


def _reached(node: ast.stmt | ast.expr, bound: Mapping[str, list[types.ModuleType]]) -> list[str]:
    if isinstance(node, ast.ImportFrom) and node.module:
        return [
            HTTP_EXCEPTION_REACH.format(how=f"the import of {node.module}.{alias.name}")
            for alias in node.names
            if _is_http_exception(_live(node.module, alias.name))
        ]
    if isinstance(node, ast.Attribute) and any(map(_is_http_exception, _resolved(node, bound))):
        return [HTTP_EXCEPTION_REACH.format(how=_dotted(node))]
    if isinstance(node, ast.Constant) and node.value == HTTP_EXCEPTION:
        return [HTTP_EXCEPTION_REACH.format(how=f"the string {HTTP_EXCEPTION!r}")]
    if isinstance(node, ast.Call):
        frameworks = _getattr_frameworks(node, bound)
        return [HTTP_EXCEPTION_UNRESOLVED.format(module=", ".join(frameworks))] if frameworks else []
    return []


def http_exception_reaches(path: Path) -> list[str]:
    """Every line of `path`, a module outside `HTTP_EXCEPTION_MODULES`, that reaches `HTTPException`
    under a name of its own, resolved against the live modules: a `from X import Y` whose `X.Y` is a
    subclass of Starlette's `HTTPException`, an attribute chain from a module the file imports that
    resolves to one, the string `"HTTPException"` (a `getattr` by a literal or implicitly concatenated
    name), and a `getattr` on a `fastapi` or `starlette` module whose name is not a literal. Not held: a
    `getattr` whose name is not a literal on any other binding."""
    tree = ast.parse(path.read_text())
    bound = _module_bindings(tree)
    found = {
        (node.lineno, message)
        for node in ast.walk(tree)
        if isinstance(node, (ast.stmt, ast.expr))
        for message in _reached(node, bound)
    }
    return [f"{_shown(path)}:{line}: {message}" for line, message in sorted(found)]


def _names_http_exception(value: ast.expr | None) -> bool:
    return value is not None and any(
        isinstance(node, ast.expr) and _is_named(node, HTTP_EXCEPTION) for node in ast.walk(value)
    )


def _called_raised_or_caught(tree: ast.Module) -> set[ast.expr]:
    kept: set[ast.expr] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            kept.add(node.func)
        elif isinstance(node, ast.Raise) and node.exc is not None:
            kept.add(node.exc)
        elif isinstance(node, ast.ExceptHandler) and node.type is not None:
            kept.update(node.type.elts if isinstance(node.type, ast.Tuple) else [node.type])
    return kept


def http_exception_rebindings(path: Path) -> list[str]:
    """Every line of `path`, an auth dependency in `HTTP_EXCEPTION_MODULES`, that binds `HTTPException`
    to a second name (an assignment whose value names it, an import under an alias, or a class that
    subclasses it) or uses it other than as a call's callee, a `raise` or an `except` clause, as a
    return value, a default or a container element does. Its import under its own name is its one
    binding. Not held: an instance a call here builds, such as `HTTPException(401)` returned from a
    function and raised by a route."""
    tree = ast.parse(path.read_text())
    kept = _called_raised_or_caught(tree)
    found = {
        (node.lineno, HTTP_EXCEPTION_HANDED)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Name, ast.Attribute))
        and isinstance(node.ctx, ast.Load)
        and _is_named(node, HTTP_EXCEPTION)
        and node not in kept
    }
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
            rebinds = _names_http_exception(node.value)
        elif isinstance(node, ast.ImportFrom):
            rebinds = any(
                alias.name == HTTP_EXCEPTION and alias.asname not in (None, HTTP_EXCEPTION)
                for alias in node.names
            )
        else:
            continue
        if rebinds:
            found.add((node.lineno, HTTP_EXCEPTION_REBOUND))
    return [f"{_shown(path)}:{line}: {message}" for line, message in sorted(found)]


def test_no_module_outside_the_auth_dependencies_reaches_http_exception_by_any_binding():
    _import_every_src_module()

    findings = [
        finding
        for path in sorted(SRC.rglob("*.py"))
        for finding in (
            http_exception_rebindings(path)
            if _shown(path) in HTTP_EXCEPTION_MODULES
            else http_exception_reaches(path)
        )
    ]

    assert findings == [], "\n".join(findings)


def test_the_runtime_zero_target_flags_reaches_rebindings_and_hand_outs_and_spares_the_allowed_uses(
    tmp_path, monkeypatch, request
):
    auth = tmp_path / "scratch_auth.py"
    auth.write_text(
        textwrap.dedent(
            """
            import fastapi
            from fastapi import HTTPException
            from fastapi import HTTPException as Denied

            Refusal = HTTPException
            Typed: type = HTTPException


            class Gone(HTTPException):
                pass


            def refusal() -> type[HTTPException]:
                return HTTPException


            def refusal_with_default(exc=fastapi.HTTPException):
                return exc


            def raises(flag):
                try:
                    if flag:
                        raise HTTPException
                    raise fastapi.HTTPException(401)
                except HTTPException:
                    raise
                except (HTTPException, ValueError):
                    pass
            """
        )
    )
    imported = tmp_path / "scratch_imported.py"
    imported.write_text("from scratch_auth import Refusal\n\n\ndef route():\n    raise Refusal(401)\n")
    fetched = tmp_path / "scratch_fetched.py"
    fetched.write_text(
        textwrap.dedent(
            """
            import fastapi


            def route(name):
                if name:
                    raise getattr(fastapi, name)(401)
                raise getattr(fastapi, "HTTP" "Exception")(401)
            """
        )
    )
    chained = tmp_path / "scratch_chained.py"
    chained.write_text("import scratch_auth as _r\n\n\ndef route():\n    raise _r.Refusal(401)\n")
    clean = tmp_path / "clean_route.py"
    clean.write_text(
        textwrap.dedent(
            """
            import fastapi
            import scratch_auth


            def route():
                return getattr(fastapi, "status"), scratch_auth.__name__
            """
        )
    )
    monkeypatch.syspath_prepend(tmp_path)
    monkeypatch.delitem(sys.modules, "scratch_auth", raising=False)
    request.addfinalizer(lambda: sys.modules.pop("scratch_auth", None))

    assert http_exception_rebindings(auth) == [
        f"{auth.as_posix()}:{line}: {message}"
        for line, message in sorted(
            [(line, HTTP_EXCEPTION_REBOUND) for line in (4, 6, 7)]
            + [(line, HTTP_EXCEPTION_HANDED) for line in (6, 7, 10, 14, 15, 18)]
        )
    ]
    assert http_exception_reaches(imported) == [
        f"{imported.as_posix()}:1: " + HTTP_EXCEPTION_REACH.format(how="the import of scratch_auth.Refusal")
    ]
    assert http_exception_reaches(fetched) == [
        f"{fetched.as_posix()}:7: " + HTTP_EXCEPTION_UNRESOLVED.format(module="fastapi"),
        f"{fetched.as_posix()}:8: " + HTTP_EXCEPTION_REACH.format(how="the string 'HTTPException'"),
    ]
    assert http_exception_reaches(chained) == [
        f"{chained.as_posix()}:5: " + HTTP_EXCEPTION_REACH.format(how="_r.Refusal")
    ]
    assert http_exception_reaches(clean) == []


def _kind_literal(call: ast.Call) -> str | None:
    for keyword in call.keywords:
        if keyword.arg == "kind":
            value = keyword.value
            return value.value if isinstance(value, ast.Constant) and isinstance(value.value, str) else None
    if len(call.args) >= 2 and isinstance(call.args[1], ast.Constant) and isinstance(call.args[1].value, str):
        return call.args[1].value
    return None


def _names_a_kind(call: ast.Call) -> bool:
    return any(keyword.arg == "kind" for keyword in call.keywords) or len(call.args) >= 2


def _raised_kinds(
    classes: Collection[ClassKey], root: Path = SRC
) -> dict[ClassKey, set[tuple[str | None, str]]]:
    """Class -> {(kind literal, 'file:line')} over every call under `root` that `_class_resolver`
    resolves to one of `classes`. A call that passes no kind records `None`: the one row of a
    one-row table. A call whose kind is a variable records nothing; the constructor judges it."""
    keys = set(classes)
    raised: dict[ClassKey, set[tuple[str | None, str]]] = {}
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text())
        resolve = _class_resolver(tree, _module_name(path, root))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            called = resolve(node.func) & keys
            kind = _kind_literal(node)
            if not called or (kind is None and _names_a_kind(node)):
                continue
            for key in called:
                raised.setdefault(key, set()).add((kind, f"{_shown(path)}:{node.lineno}"))
    return raised


def _unknown_kinds(tables: Mapping[ClassKey, Collection[str]], root: Path = SRC) -> list[str]:
    return [
        f"{where}: {'.'.join(key)} has no kind {kind!r}; the table is {sorted(tables[key])}"
        for key, sites in sorted(_raised_kinds(tables, root).items())
        for kind, where in sorted(sites, key=lambda site: (site[0] or "", site[1]))
        if kind is not None and kind not in tables[key]
    ]


def test_every_app_error_subclass_declares_domain_and_kinds():
    _import_every_src_module()

    problems = []
    for cls in _src_subclasses():
        shown = f"{cls.__module__}.{cls.__qualname__}"
        domain = getattr(cls, "domain", "")
        kinds = getattr(cls, "kinds", {})
        if not (isinstance(domain, str) and domain):
            problems.append(f"{shown}: set `domain` to the service's name")
        if not kinds:
            problems.append(f"{shown}: set `kinds` to a non-empty table of kind -> Outcome")
        problems.extend(
            f"{shown}: kind {kind!r} has status {outcome.status}; an error is 400 <= status < 600"
            for kind, outcome in kinds.items()
            if not 400 <= outcome.status < 600
        )
    assert problems == [], "\n".join(problems)


def test_every_raised_kind_is_in_its_class_table():
    tables = {key: cls.kinds for key, cls in _app_error_classes().items()}

    problems = _unknown_kinds(tables)

    assert problems == [], "\n".join(problems)


def test_kind_tables_are_keyed_by_module_and_qualname(tmp_path):
    for package in ("pkg_a", "pkg_b"):
        (tmp_path / package).mkdir()
        (tmp_path / package / "errors.py").write_text("class TwinError(AppError):\n    pass\n")
    use = tmp_path / "pkg_a" / "use.py"
    use.write_text(
        textwrap.dedent(
            """
            import pkg_b.errors
            import pkg_b.errors as b_errors
            from pkg_a.errors import TwinError
            from pkg_b.errors import TwinError as OtherTwin


            def route():
                TwinError("m", kind="a")
                TwinError("m", kind="b")
                OtherTwin("m", kind="b")
                b_errors.TwinError("m", kind="a")
                pkg_b.errors.TwinError("m", kind="b")
            """
        )
    )
    tables = {("pkg_a.errors", "TwinError"): ["a"], ("pkg_b.errors", "TwinError"): ["b"]}

    assert _unknown_kinds(tables, tmp_path) == [
        f"{use.as_posix()}:10: pkg_a.errors.TwinError has no kind 'b'; the table is ['a']",
        f"{use.as_posix()}:12: pkg_b.errors.TwinError has no kind 'a'; the table is ['b']",
    ]


def test_a_re_exported_class_is_judged_against_its_defining_table(tmp_path, monkeypatch):
    (tmp_path / "scratch_errors.py").write_text(
        textwrap.dedent(
            """
            from core.errors import AppError, Outcome


            class ReExportedError(AppError):
                domain = "scratch"
                kinds = {"gone": Outcome(410, None)}
            """
        )
    )
    (tmp_path / "scratch_hub.py").write_text("from scratch_errors import ReExportedError\n")
    use = tmp_path / "scratch_route.py"
    use.write_text(
        textwrap.dedent(
            """
            import scratch_hub
            from scratch_hub import ReExportedError


            def route(flag):
                if flag:
                    raise ReExportedError("x", kind="missing")
                raise scratch_hub.ReExportedError("x", kind="missing")
            """
        )
    )
    monkeypatch.syspath_prepend(tmp_path)
    for name in ("scratch_errors", "scratch_hub"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    tables = {("scratch_errors", "ReExportedError"): ["gone"]}
    unknown = "scratch_errors.ReExportedError has no kind 'missing'; the table is ['gone']"

    assert _unknown_kinds(tables, tmp_path) == [f"{use.as_posix()}:{line}: {unknown}" for line in (8, 9)]


def test_every_table_row_is_raised_somewhere():
    classes = _app_error_classes()
    raised = {key: {kind for kind, _ in sites} for key, sites in _raised_kinds(classes).items()}

    problems = [
        f"{'.'.join(key)}: kind {kind!r} is a dead row; no raise in src/ names it"
        for key, cls in classes.items()
        for kind in sorted(cls.kinds)
        if kind not in raised.get(key, set()) and not (len(cls.kinds) == 1 and None in raised.get(key, set()))
    ]
    assert problems == [], "\n".join(problems)


class TableRow(NamedTuple):
    shown: str
    outcome: Outcome


def _table_codes() -> dict[str, list[TableRow]]:
    codes: dict[str, list[TableRow]] = {}
    for cls in _src_subclasses():
        shown = f"{cls.__module__}.{cls.__qualname__}"
        for kind, outcome in cls.kinds.items():
            codes.setdefault(f"{cls.domain}.{kind}", []).append(TableRow(shown, outcome))
    return codes


def _document_codes_as_production_imports_them() -> list[str]:
    result = subprocess.run(
        [sys.executable, "-c", DOCUMENT_CODES], cwd=SRC, capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_defined_under_src_accepts_a_src_module_and_refuses_a_tests_module(monkeypatch):
    monkeypatch.delitem(sys.modules, "scratch_errors", raising=False)
    expected = {
        "main": True,
        "core.rbac": True,
        "core": True,
        "api.routes": True,
        "core.zz_planted": False,
        "scratch_errors": False,
        "": False,
        __name__: False,
    }

    for predicate in (core.errors._defined_under_src, _defined_under_src):
        judged = {name: predicate(type("Planted", (), {"__module__": name})) for name in expected}
        assert judged == expected, predicate.__module__


def test_the_document_keeps_every_code_after_the_defining_modules_leave_sys_modules(monkeypatch):
    _import_every_src_module()
    expected = sorted(_table_codes())
    for module in sorted({cls.__module__ for cls in _src_subclasses()}):
        monkeypatch.delitem(sys.modules, module, raising=False)
    monkeypatch.setattr(app, "openapi_schema", None)

    codes = app.openapi()["components"]["schemas"]["ErrorBody"]["properties"]["code"]["enum"]

    assert "auth.role_required" in codes
    assert codes == expected


def test_the_document_carries_every_code():
    _import_every_src_module()
    expected = sorted(_table_codes())

    in_document = _document_codes_as_production_imports_them()

    missing = sorted(set(expected) - set(in_document))
    assert missing == [], (
        f"the document's ErrorBody.code enum lacks {missing}: the class that defines it lives in a module "
        "`main` never imports; import that module at module scope where its route lives"
    )
    assert in_document == expected


def test_the_document_requires_code():
    schema = app.openapi()["components"]["schemas"]["ErrorBody"]
    code = schema["properties"]["code"]

    assert schema["required"] == ["detail", "code"], (
        "every route refusal raises an AppError kind, so every declared error body carries `code`"
    )
    assert "anyOf" not in code and "default" not in code, code
    assert code["type"] == "string" and None not in code["enum"], code


def test_every_code_has_exactly_one_outcome():
    _import_every_src_module()

    clashes = [
        f"{code} is {rows[0].outcome} on {rows[0].shown} and {row.outcome} on {row.shown}; "
        "one code, one outcome"
        for code, rows in sorted(_table_codes().items())
        for row in rows[1:]
        if row.outcome != rows[0].outcome
    ]
    assert clashes == [], "\n".join(clashes)


def _row_instance(cls: type[AppError], kind: str) -> AppError:
    exc = cls.__new__(cls)
    AppError.__init__(exc, "the message", kind)
    return exc


def test_every_table_row_body_validates_through_error_body():
    _import_every_src_module()

    for cls in _src_subclasses():
        for kind in cls.kinds:
            body = _row_instance(cls, kind).body()
            assert ErrorBody.model_validate(body).model_dump() == body, f"{cls.__qualname__}.{kind}"
