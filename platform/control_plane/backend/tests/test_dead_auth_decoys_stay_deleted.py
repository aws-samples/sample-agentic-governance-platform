"""Guard: the deleted auth decoys must not come back (E36/T14).

Four things in this backend used to *look* like the authentication path without being on it, and
each one cost a reader real time before it was recognised as dead:

  - `core/auth.py` — an import-time dispatcher exporting `get_current_user` / `require_admin`,
    imported by nothing.
  - `core/dev_auth.py` — its NAME promised the dev bypass and its docstring claimed to read
    `x-user-email`, but the body returned the admin fixture unconditionally. Read it to learn how
    the bypass works and you learned something false about a file that never ran.
  - `core/security_entra_facade.py` — a `Depends(HTTPBearer())` wrapper around the validator,
    reachable from no route, carrying a THIRD copy of the email-precedence chain that could drift
    from the two live ones unnoticed.
  - `StripPathPrefixMiddleware` in `main.py` — rewrote `scope["path"]` to strip `ROOT_PATH`, never
    instantiated, never `add_middleware`-d.

The live inbound path is `core/rbac.py` → `core/security_entra.py`, and the stage prefix works by
registering every router twice (bare and under `ROOT_PATH`) at the bottom of `main.py` — NOT by
rewriting paths in middleware.

A source/filesystem check rather than an import check, deliberately: the failure mode being pinned
is a file REAPPEARING (restored from a stale branch, re-created by someone following an old plan
doc), and an import assertion can only speak about files that already exist. `test_no_token_logging`
and `test_wire_logger_clamp` guard their invariants the same way and for the same reason.

Everything below reads the AST, never the raw text: each deletion in this repo leaves a tombstone
COMMENT naming what went and why (`main.py`'s stripped-middleware and `/test`/`/health` notes), and
a text-substring guard would fire on the tombstone that documents the fix — which would teach the
next reader to delete the explanation in order to go green.

The dev bypass that IS live — `USE_DEV_AUTH` / `DEBUG` in `core/rbac.py` — is deliberately NOT
touched by this file. Deleting `dev_auth.py` did not remove it; see `test_rbac_entra.py`.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
_MAIN_PY = _SRC / "main.py"

# Every module deleted in E36/T14, by path relative to `src/`.
_DELETED_MODULES = ("core/auth.py", "core/dev_auth.py", "core/security_entra_facade.py")

# The importable names those files occupied. Both spellings are listed because `src` is on the path,
# so `core.dev_auth` and a bare `dev_auth` would both resolve.
_DEAD_MODULE_NAMES = frozenset(
    {"auth", "dev_auth", "security_entra_facade"}
    | {f"core.{name}" for name in ("auth", "dev_auth", "security_entra_facade")}
)


# The ASGI primitives the deleted middleware class needed. Matched by terminal name, so the ban
# holds whether they arrive from `starlette.types`, `starlette.middleware.base`, or FastAPI's
# re-export of the same objects.
_ASGI_PRIMITIVES = frozenset({"ASGIApp", "Receive", "Scope", "Send", "BaseHTTPMiddleware"})


def _imported_modules(tree: ast.Module) -> set[str]:
    """Every module name imported by `tree`, in the spelling the source used.

    `from . import x` / relative imports carry `level > 0` and no dotted name to compare; this
    backend imports absolutely everywhere (`from core.config import settings`), so they are
    reported as-is and simply never match the dead names.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            # `from core import auth` — the module is the package, the symbol is the file.
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def _middleware_class_name(call: ast.Call) -> str | None:
    """The class an `add_middleware` call installs, or `None` if no name can be read off it.

    `add_middleware` accepts the class positionally or as `middleware_class=`, and either spelling
    may be dotted (`smb.BaseHTTPMiddleware`); all four forms install middleware, so all four have to
    be counted. The terminal name is returned rather than the dotted path, because the invariant is
    about WHICH class is installed, not how the file happened to import it.

    `None` means the class is an expression (a variable, a factory call) — the caller must surface
    that as a failure, not skip it, or the count below stops being a bound at all.
    """
    node = next(
        (kw.value for kw in call.keywords if kw.arg == "middleware_class"),
        call.args[0] if call.args else None,
    )
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _is_middleware_shaped(node: ast.ClassDef) -> bool:
    """Whether `node` is an ASGI middleware in shape, whatever it happens to be called.

    The deleted `StripPathPrefixMiddleware` was a BARE ASGI class — no base class at all, an
    `__init__` taking the wrapped `app`, and `async def __call__(self, scope, receive, send)`. The
    `__call__`-shape test is the load-bearing one, because this file's whole premise is that a decoy
    can come back under any spelling; the name suffix and the `dispatch` hook (the only reason to
    subclass `BaseHTTPMiddleware`) cover the two likelier re-additions.

    Deliberately narrow: a response model, an Enum, or a custom exception in `main.py` is ordinary
    and is none of this test's business.
    """
    if node.name.endswith("Middleware"):
        return True
    for member in node.body:
        if not isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if member.name == "dispatch":
            return True
        if member.name == "__call__" and [arg.arg for arg in member.args.args[1:]] == [
            "scope",
            "receive",
            "send",
        ]:
            return True
    return False


@pytest.mark.parametrize("module", _DELETED_MODULES)
def test_deleted_auth_module_does_not_exist(module):
    """The three dead auth modules stay deleted."""
    assert not (_SRC / module).exists(), (
        f"src/{module} is back. The live inbound path is core/rbac.py -> core/security_entra.py; "
        "a second one that looks authoritative is the defect this file exists to stop."
    )


def test_no_source_file_imports_the_deleted_auth_modules():
    """Nothing under `src/` imports the deleted modules — including a re-added copy of them.

    Guards the other direction from the test above: a resurrected file with a NEW name is only a
    problem once something imports it, and a resurrected file with the OLD name would break here
    loudly (with the importer named) instead of at container start.
    """
    offenders = sorted(
        str(path.relative_to(_SRC))
        for path in _SRC.rglob("*.py")
        if _imported_modules(ast.parse(path.read_text())) & _DEAD_MODULE_NAMES
    )
    assert offenders == [], f"these files import a module deleted in E36/T14: {offenders}"


def test_main_adds_exactly_one_middleware_and_declares_no_asgi_class():
    """`main.py` wires CORS and nothing else, and declares no path-rewriting ASGI class.

    The `add_middleware` COUNT is the assertion, not the absence of the old class name: a re-add
    under any other spelling still has to call `add_middleware` to take effect, so one call is the
    tighter bound — and a class that is declared but never added is precisely the decoy that was
    deleted, so both halves are checked. EVERY `add_middleware` call is counted, including the ones
    whose class this test cannot name; an uncountable call is a failure, because a call that gets
    skipped silently is the one hole that would make the count meaningless.
    """
    tree = ast.parse(_MAIN_PY.read_text())

    added = [
        _middleware_class_name(node) or f"<unnameable middleware at main.py:{node.lineno}>"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "add_middleware"
    ]
    assert added == ["CORSMiddleware"], (
        f"main.py should add exactly one middleware (CORS); found {added}"
    )

    shaped = [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and _is_middleware_shaped(node)
    ]
    assert shaped == [], (
        "main.py declares a middleware-shaped class — dead unless it is also passed to "
        f"add_middleware, which is how the last one went unnoticed. Found: {shaped}"
    )


def test_starlette_low_level_imports_are_gone_from_main():
    """The ASGI-primitive imports existed only for the deleted middleware class.

    Left behind they are the seed of the next decoy: the class is easiest to re-add when its
    imports (`ASGIApp`, `Receive`, `Scope`, `Send`, `BaseHTTPMiddleware`) are already sitting at
    the top of the file. Those five names are the assertion, under any import spelling including
    FastAPI's own re-exports (`fastapi.middleware.base`) — the rest of `starlette` is not implicated
    and stays available, so `from starlette.status import HTTP_200_OK` is nobody's decoy.
    """
    imported = _imported_modules(ast.parse(_MAIN_PY.read_text()))

    offenders = sorted(
        name for name in imported if name.rsplit(".", 1)[-1] in _ASGI_PRIMITIVES
    )
    assert offenders == [], f"main.py imports ASGI primitives again: {offenders}"
