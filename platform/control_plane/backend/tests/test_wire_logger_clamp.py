"""Guard: `LOG_LEVEL=DEBUG` must not switch on third-party HTTP-wire logging.

`LOG_LEVEL` is an operator-settable field (`core/config.py`) and `main.configure_logging` calls
`logging.basicConfig`, which configures the **ROOT** logger — so without a clamp `LOG_LEVEL=DEBUG`
turns DEBUG on for every library in the process. That matters because `botocore.parsers` logs each
response verbatim (`LOG.debug("Response body:\n%r", response["body"])`) and every credential this
backend touches arrives as a boto3 Secrets Manager response body: GitHub PATs / App private-key
PEMs (`services/connection_service.py`), per-user GitHub OAuth tokens
(`services/github_user_link.py`), Langfuse public/secret keys (`services/langfuse_provisioning.py`).
A `GetSecretValue` body IS the secret, so one `LOG_LEVEL=DEBUG` deployment would write live
credentials to CloudWatch.

These tests assert EFFECTIVE LEVELS, not that a function was called — the effective level is the
only thing that decides whether a record is emitted, and `botocore.parsers` inherits it from the
clamped `botocore` parent rather than carrying one of its own.

Note on the fixture: CPython's `basicConfig` applies its `level=` only when root has no handlers
yet, so the import-time conditions have to be reproduced by clearing root around the call (pytest's
logging plugin installs a root handler for every test phase). Everything is restored afterwards —
logging is global state and the rest of the suite shares it.
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path

import pytest

from main import _WIRE_LOGGERS, configure_logging

# The four reference agents configure logging themselves from a `LOG_LEVEL` env var their READMEs
# advertise, and they read the same Langfuse secret with boto3 — so they need the same clamp. They
# cannot be imported here (their deps, `strands`/`bedrock_agentcore`, are not in the backend image),
# so the guard is a source check, the same approach `test_no_token_logging.py` takes for the same
# reason: the agents are near-clones, so a fix in one file does not stay fixed.
APPS_DIR = Path(__file__).resolve().parents[4] / "applications"
AGENT_FILES = sorted(APPS_DIR.glob("acme_*_agent/agent.py"))


@pytest.fixture
def configure_logging_from_scratch():
    """Yield a callable that runs `main.configure_logging` under import-time conditions.

    CPython's `basicConfig` applies its `level=` only when root has no handlers yet, and pytest's
    logging plugin installs one for every test phase — so root is cleared for the call and pytest's
    handlers are put straight back. Only LEVELS are under test, so the handler `basicConfig` creates
    is discarded. Levels are restored on teardown; logging is global state.
    """
    root = logging.getLogger()
    saved_level = root.level
    saved_wire = {name: logging.getLogger(name).level for name in _WIRE_LOGGERS}

    def _run(log_level: str) -> None:
        stashed = root.handlers[:]
        root.handlers[:] = []
        try:
            configure_logging(log_level)
        finally:
            root.handlers[:] = stashed

    try:
        yield _run
    finally:
        root.setLevel(saved_level)
        for name, level in saved_wire.items():
            logging.getLogger(name).setLevel(level)


def test_debug_log_level_keeps_botocore_above_debug(configure_logging_from_scratch):
    """The headline guarantee: our code at DEBUG (10), botocore no lower than INFO (20)."""
    configure_logging_from_scratch("DEBUG")

    assert logging.getLogger("main").getEffectiveLevel() == logging.DEBUG == 10
    assert logging.getLogger("services.connection_service").getEffectiveLevel() == 10
    assert logging.getLogger("botocore").getEffectiveLevel() == logging.INFO == 20
    assert logging.getLogger("botocore").getEffectiveLevel() > logging.DEBUG


def test_debug_log_level_keeps_the_payload_bearing_child_loggers_above_debug(configure_logging_from_scratch):
    """`botocore.parsers` is the logger that actually prints the secret; it has no level of its
    own, so this pins that it inherits the clamp from `botocore`."""
    configure_logging_from_scratch("DEBUG")

    for name in ("botocore.parsers", "botocore.endpoint", "botocore.awsrequest"):
        assert logging.getLogger(name).getEffectiveLevel() == 20, name


def test_debug_log_level_clamps_every_declared_wire_logger(configure_logging_from_scratch):
    configure_logging_from_scratch("DEBUG")

    assert logging.getLogger("botocore").getEffectiveLevel() == 20
    for name in _WIRE_LOGGERS:
        assert logging.getLogger(name).getEffectiveLevel() == 20, name


def test_clamp_is_a_floor_and_never_raises_verbosity(configure_logging_from_scratch):
    """A quieter `LOG_LEVEL` must be left alone — the clamp only ever removes output. Setting
    botocore to a fixed INFO here would newly EMIT botocore INFO records under LOG_LEVEL=WARNING."""
    configure_logging_from_scratch("WARNING")

    assert logging.getLogger("main").getEffectiveLevel() == logging.WARNING == 30
    for name in _WIRE_LOGGERS:
        assert logging.getLogger(name).getEffectiveLevel() == 30, name


def test_default_info_log_level_is_unchanged_by_the_clamp(configure_logging_from_scratch):
    """At the shipped default the clamp is a no-op, so it carries no behaviour change today."""
    configure_logging_from_scratch("INFO")

    assert logging.getLogger("main").getEffectiveLevel() == 20
    for name in _WIRE_LOGGERS:
        assert logging.getLogger(name).getEffectiveLevel() == 20, name


# -- the four reference agents ------------------------------------------------------------------


def _clamp_loop(agent_file: Path) -> ast.For:
    """Return the module-level `for` loop that clamps the wire loggers, or fail."""
    for node in ast.parse(agent_file.read_text()).body:
        if isinstance(node, ast.For) and isinstance(node.iter, (ast.Tuple, ast.List)):
            names = {e.value for e in node.iter.elts if isinstance(e, ast.Constant)}
            if "botocore" in names:
                return node
    pytest.fail(
        f"{agent_file.relative_to(APPS_DIR)} no longer clamps the third-party wire loggers. "
        "LOG_LEVEL=DEBUG there makes botocore.parsers log every response body, including the "
        "GetSecretValue body carrying this agent's Langfuse secret key. Restore the clamp."
    )


def test_the_four_reference_agents_exist():
    """Pins the glob, so the source guards below cannot pass by matching nothing."""
    assert len(AGENT_FILES) == 4, [str(p) for p in AGENT_FILES]


@pytest.mark.parametrize("agent_file", AGENT_FILES, ids=lambda p: p.parent.name)
def test_reference_agent_clamps_the_same_wire_loggers_at_info(agent_file: Path):
    loop = _clamp_loop(agent_file)
    clamped = {e.value for e in loop.iter.elts if isinstance(e, ast.Constant)}
    assert clamped == set(_WIRE_LOGGERS), f"{agent_file.parent.name}: {clamped}"

    body = ast.unparse(loop.body)
    assert "setLevel(logging.INFO)" in body, f"{agent_file.parent.name}: {body}"
    # A floor, not a fixed level — same semantics as the backend's `configure_logging`.
    assert "getEffectiveLevel() < logging.INFO" in body, f"{agent_file.parent.name}: {body}"
