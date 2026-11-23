"""``DEBUG`` is logging only: it raises the log level, and nothing else reads it.

A 500 body is one fixed shape for every caller, ``DEBUG`` on or off: the unhandled exception goes to the
log at ERROR with its traceback and never to the caller, so a connection string or a provider sentence
in its text stays inside the task. The source of the handler the app registers for ``Exception`` holds that
for every caller and exception type: it logs and returns the response, nothing else; the response is a
status and a body of constants, no header; and the exception is read only inside the ``logger.error`` call.
``DEBUG`` on resolves ``LOG_LEVEL`` to ``DEBUG`` and changes no other setting; off, ``LOG_LEVEL`` is honoured
as set. And the one reader of ``DEBUG`` under ``src/``, by attribute or by the ``"DEBUG"`` string
a ``getattr`` or an ``os.environ`` lookup takes, is that resolution in ``Settings.__init__``.
"""

import ast
import logging
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.config import Settings

SRC = Path(__file__).resolve().parents[1] / "src"
HANDLER = "global_exception_handler"
LEAKY = "postgres://user:hunter2@db.internal:5432 refused the connection"
FIXED_500 = {"detail": "Internal server error", "error": "An error occurred"}
THE_ONE_READER = {("core/config.py", "__init__")}


def _app_with_mains_handlers(exc: Exception) -> TestClient:
    import main as main_module

    app = FastAPI()
    for exc_type, handler in main_module.app.exception_handlers.items():
        app.add_exception_handler(exc_type, handler)
    app.user_middleware = list(main_module.app.user_middleware)

    @app.get("/boom")
    async def boom():
        raise exc

    return TestClient(app, raise_server_exceptions=False)


def _function(path: Path, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    return next(
        node
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name
    )


def _is_logger_error(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "error"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "logger"
    )


def _reads_debug(node: ast.AST) -> bool:
    return (isinstance(node, ast.Attribute) and node.attr == "DEBUG") or (
        isinstance(node, ast.Constant) and node.value == "DEBUG"
    )


def _debug_readers(tree: ast.AST) -> set[str]:
    """The innermost function around each ``DEBUG`` read, ``<module>`` for a read outside every function."""
    owners: set[str] = set()

    def visit(node: ast.AST, owner: str) -> None:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            owner = node.name
        if _reads_debug(node):
            owners.add(owner)
        for child in ast.iter_child_nodes(node):
            visit(child, owner)

    visit(tree, "<module>")
    return owners


@pytest.mark.parametrize("debug", [True, False], ids=["debug_on", "debug_off"])
def test_a_500_body_is_one_fixed_shape_for_every_caller(monkeypatch, caplog, debug):
    import main as main_module

    monkeypatch.setattr(main_module.settings, "DEBUG", debug)
    with caplog.at_level(logging.ERROR, logger="main"):
        resp = _app_with_mains_handlers(RuntimeError(LEAKY)).get("/boom")
    assert resp.status_code == 500, resp.text
    assert resp.json() == FIXED_500
    assert "hunter2" not in resp.text
    logged = [
        record for record in caplog.records if record.name == "main" and "hunter2" in record.getMessage()
    ]
    assert logged and all(record.exc_info for record in logged), logged


def test_the_500_handler_builds_a_constant_body_and_hands_the_exception_to_the_log_alone():
    import main as main_module

    registered = main_module.app.exception_handlers[Exception]
    assert registered.__name__ == HANDLER, registered
    handler = _function(SRC / "main.py", HANDLER)
    assert [ast.unparse(d) for d in handler.decorator_list] == ["app.exception_handler(Exception)"]
    exception = handler.args.args[1].arg
    assert len(handler.body) == 3, [ast.unparse(statement) for statement in handler.body]
    doc, log, returned = handler.body
    assert isinstance(doc, ast.Expr) and isinstance(doc.value, ast.Constant), ast.unparse(doc)
    assert isinstance(log, ast.Expr) and _is_logger_error(log.value), ast.unparse(log)
    assert isinstance(returned, ast.Return) and isinstance(returned.value, ast.Call), ast.unparse(returned)
    cors = returned.value
    assert isinstance(cors.func, ast.Name) and cors.func.id == "_with_cors", ast.unparse(cors)
    assert len(cors.args) == 2 and not cors.keywords, ast.unparse(cors)
    request, response = cors.args
    assert isinstance(request, ast.Name) and request.id == "request", ast.unparse(cors)
    assert isinstance(response, ast.Call) and isinstance(response.func, ast.Name), ast.unparse(cors)
    assert response.func.id == "JSONResponse", ast.unparse(cors)
    keywords = {keyword.arg for keyword in response.keywords}
    assert not response.args and keywords == {"status_code", "content"}, ast.unparse(response)
    [content] = [keyword.value for keyword in response.keywords if keyword.arg == "content"]
    assert isinstance(content, ast.Dict), ast.unparse(content)
    parts = [*content.keys, *content.values]
    assert all(isinstance(part, ast.Constant) for part in parts), ast.unparse(content)
    assert ast.literal_eval(content) == FIXED_500
    in_the_log = {id(node) for node in ast.walk(log)}
    reads = [node for node in ast.walk(handler) if isinstance(node, ast.Name) and node.id == exception]
    outside = [node.lineno for node in reads if id(node) not in in_the_log]
    assert reads and not outside, outside


def test_debug_on_resolves_the_log_level_to_debug_and_off_leaves_it_as_set(monkeypatch):
    assert Settings(DEBUG=False, LOG_LEVEL="WARNING").LOG_LEVEL == "WARNING"
    assert Settings(DEBUG=True, LOG_LEVEL="WARNING").LOG_LEVEL == "DEBUG"
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    monkeypatch.setenv("DEBUG", "false")
    assert Settings().LOG_LEVEL == "WARNING"
    monkeypatch.setenv("DEBUG", "true")
    assert Settings().LOG_LEVEL == "DEBUG"


def test_debug_changes_no_setting_but_the_log_level():
    on = Settings(DEBUG=True, LOG_LEVEL="WARNING").model_dump()
    off = Settings(DEBUG=False, LOG_LEVEL="WARNING").model_dump()
    assert {name for name in on if on[name] != off[name]} == {"DEBUG", "LOG_LEVEL"}


def test_debug_is_read_nowhere_in_src_but_the_log_level_resolution():
    readers = {
        (str(path.relative_to(SRC)), owner)
        for path in SRC.rglob("*.py")
        for owner in _debug_readers(ast.parse(path.read_text()))
    }
    assert readers == THE_ONE_READER, sorted(readers)
