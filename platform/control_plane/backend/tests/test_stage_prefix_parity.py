"""Every router is served bare and under the stage prefix, registered in one place.

API Gateway forwards the stage segment, so the backend serves each path twice: bare, for local runs,
and under `ROOT_PATH`, for every deployed environment. A router registered bare only passes every
local test and 404s in the cloud. `ROOT_PATH` is empty in the suite, so the app is re-imported with
a stage set; FastAPI's own documentation routes are registered once, bare, and are left out.
"""

import ast
import sys
from contextlib import contextmanager
from pathlib import Path

from conftest import app_route_paths

SRC = Path(__file__).resolve().parents[1] / "src"
MAIN = SRC / "main.py"
STAGE = "/dev"
REGISTRATION_FUNCTION = "_register_routers"
FASTAPI_DOCUMENTATION_ROUTES = frozenset({"/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"})

_RELOAD_PREFIXES = ("api.", "core.config")


@contextmanager
def _main_reloaded_under_the_stage(monkeypatch):
    monkeypatch.setenv("ROOT_PATH", STAGE)

    def _evict():
        for key in [k for k in sys.modules if k == "main" or k.startswith(_RELOAD_PREFIXES)]:
            sys.modules.pop(key, None)

    _evict()
    try:
        import main

        yield main
    finally:
        _evict()


def _src_trees() -> dict[Path, ast.Module]:
    return {path: ast.parse(path.read_text(), filename=str(path)) for path in sorted(SRC.rglob("*.py"))}


def _registration_function(main_tree: ast.Module) -> ast.FunctionDef:
    found = [
        node
        for node in main_tree.body
        if isinstance(node, ast.FunctionDef) and node.name == REGISTRATION_FUNCTION
    ]
    assert len(found) == 1, f"main.py defines {len(found)} module-level {REGISTRATION_FUNCTION}, not one"
    return found[0]


def _include_router_calls(trees: dict[Path, ast.Module]) -> list[tuple[str, ast.Call]]:
    return [
        (f"{path.relative_to(SRC)}:{node.lineno}", node)
        for path, tree in trees.items()
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "include_router"
    ]


def test_every_bare_path_is_served_under_the_stage_prefix_and_nothing_else_is(monkeypatch):
    with _main_reloaded_under_the_stage(monkeypatch) as main:
        paths = app_route_paths(main.app)

    staged = {path.removeprefix(STAGE) for path in paths if path.startswith(f"{STAGE}/")}
    bare = {path for path in paths if not path.startswith(f"{STAGE}/")} - FASTAPI_DOCUMENTATION_ROUTES

    assert staged, f"no path under {STAGE}: the reload did not take"
    assert sorted(bare - staged) == [], f"served bare only, 404 behind the stage: {sorted(bare - staged)}"
    assert sorted(staged - bare) == [], f"served behind the stage only, 404 locally: {sorted(staged - bare)}"


def test_include_router_is_named_only_inside_the_one_registration_function():
    """An `include_router` attribute, or the name as a string a `getattr` would take, outside
    `main._register_routers` is a second registration path the parity test cannot see coming."""
    trees = _src_trees()
    inside = {id(node) for node in ast.walk(_registration_function(trees[MAIN]))}

    outside = [
        f"{path.relative_to(SRC)}:{node.lineno}"
        for path, tree in trees.items()
        for node in ast.walk(tree)
        if id(node) not in inside
        and (
            (isinstance(node, ast.Attribute) and node.attr == "include_router")
            or (isinstance(node, ast.Constant) and node.value == "include_router")
        )
    ]

    assert outside == [], f"include_router named outside main.{REGISTRATION_FUNCTION}: {outside}"


def test_the_registration_passes_a_prefix_and_nothing_else():
    """A `dependencies=` on the call would move authorization off the endpoint."""
    trees = _src_trees()
    calls = _include_router_calls(trees)
    assert calls, "no include_router call under src: the scan reads nothing"

    called = {id(call.func) for _, call in calls}
    as_a_value = [
        f"{path.relative_to(SRC)}:{node.lineno}"
        for path, tree in trees.items()
        for node in ast.walk(tree)
        if (isinstance(node, ast.Attribute) and node.attr == "include_router" and id(node) not in called)
        or (isinstance(node, ast.Constant) and node.value == "include_router")
    ]
    assert as_a_value == [], f"include_router reached as a value, its keywords unread: {as_a_value}"

    keywords = [(where, [kw.arg for kw in call.keywords]) for where, call in calls]
    other = [(where, names) for where, names in keywords if names != ["prefix"]]

    assert other == [], f"include_router called with keywords other than prefix alone: {other}"
