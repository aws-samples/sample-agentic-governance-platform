"""Regenerate the captured strands-agentcore invoke SSE fixtures (E28D/T3).

WHY THIS EXISTS. The template (the PRODUCER) and ``_extract_sse_text`` (the CONSUMER) live in
trees that cannot import each other, so each side used to be tested against its own *model* of
the other — which is how a stream that showed a single emoji as the whole answer shipped. These
fixtures are the shared artifact: a body produced by the real template handler, asserted by the
platform's parser. Hand-editing them defeats the entire point — regenerate instead.

HOW TO REGENERATE (offline, no AWS: the agent is stubbed exactly as the template's own tests
stub it). From the repo root:

    uv run --project platform/control_plane/agent-templates/strands-agentcore \
        python platform/control_plane/backend/tests/fixtures/generate_strands_agentcore_fixtures.py

EXPECTED NOISE: the run prints a ``RuntimeError: ThrottlingException: rate exceeded`` traceback (that
IS the scenario being captured) plus an ``AttributeError: 'ProxyTracerProvider' object has no
attribute 'force_flush'`` and an "OTEL span flush failed" warning (no exporter is configured
offline). None of that is a failure — success is the two ``wrote …`` lines and exit 0.

The ``produced by`` commit is stamped automatically, ``-dirty`` included when the template tree has
uncommitted changes. Re-run ``tests/test_invoke_route.py`` afterwards. If a fence test now fails, the
contract changed: decide which side is wrong before touching the expected string.

The encoding is AgentCore's own: ``bedrock_agentcore.runtime.app._convert_to_sse`` writes
``f"data: {json.dumps(obj, ensure_ascii=False)}\\n\\n"`` — note ``ensure_ascii=False``, which is
why the emoji is literal UTF-8 on the wire rather than an escape.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

FIXTURE_DIR = Path(__file__).resolve().parent
TEMPLATE_ROOT = (FIXTURE_DIR / "../../../agent-templates/strands-agentcore").resolve()

sys.path.insert(0, str(TEMPLATE_ROOT))

# Imported after the sys.path insert above, deliberately (E402): the template ships standalone, so
# `src.main` only resolves once TEMPLATE_ROOT is on the path.
import src.main as main_module
from src.main import handler


def _load_template_tests():
    """Import the template's own test module for its pinned stub events.

    Imported rather than copied on purpose: ``_STRANDS_EVENTS`` and the exact terminal string are
    the producer's pins, and a second transcription here would be a fifth model of the contract.
    """
    spec = importlib.util.spec_from_file_location(
        "_template_test_agent", TEMPLATE_ROOT / "tests" / "test_agent.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _drain(agent_stub, prompt="math please"):
    main_module.agent = agent_stub

    async def _collect():
        return [chunk async for chunk in handler({"prompt": prompt})]

    return asyncio.run(_collect())


def _encode_sse(chunks) -> str:
    return "".join(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n" for chunk in chunks)


def _git(*args: str) -> str:
    # Fixed argv, no shell. `check=False` on purpose: a git failure must degrade the stamp to
    # UNKNOWN, not abort a regeneration that is otherwise fine.
    out = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        cwd=TEMPLATE_ROOT,
        check=False,
    )
    return out.stdout.strip()


def _template_commit() -> str:
    """The template's last commit, suffixed ``-dirty`` when its tree has uncommitted changes.

    The dirty check is the load-bearing half. ``git log`` alone names the last COMMITTED commit,
    which is not necessarily the code that produced these bytes: regenerate with an edited
    ``handler`` (or an edited ``_STRANDS_EVENTS``) and the stamp would name a commit that provably
    does NOT produce the fixture. Someone debugging a fence failure would then check out that
    commit, regenerate, get different bytes, and blame the fence rather than the stamp.

    A ``-dirty`` suffix is deliberately preferred over refusing to write: the fixtures are usually
    regenerated in the same working tree as the change that motivated them, so refusing would block
    the normal workflow. Once that change is committed, a regeneration drops the suffix.
    """
    commit = _git("log", "-1", "--format=%h", "--", str(TEMPLATE_ROOT)) or "UNKNOWN"
    dirty = _git("status", "--porcelain", "--", str(TEMPLATE_ROOT))
    return f"{commit}-dirty" if dirty else commit


def _write(name: str, header: list[str], body: str) -> None:
    commit = _template_commit()
    lines = [
        "# CAPTURED WIRE BODY — do not hand-edit. See regeneration note below.",
        f"# produced by: agent-templates/strands-agentcore @ commit {commit}",
        "# regenerate:  uv run --project platform/control_plane/agent-templates/strands-agentcore \\",
        (
            "#                python platform/control_plane/backend/tests/fixtures/"
            "generate_strands_agentcore_fixtures.py"
        ),
        "#              (offline; the agent is stubbed. The commit above is stamped automatically.)",
        "# A '-dirty' suffix means the template tree had uncommitted changes when this was captured,",
        "# so the named commit alone does not reproduce these bytes. Regenerating after the template",
        "# change is committed normalizes the stamp.",
        *[f"# {line}" for line in header],
        "",
    ]
    (FIXTURE_DIR / name).write_text("\n".join(lines) + body, encoding="utf-8")
    print(f"wrote {name} ({len(body)} bytes of body) @ template commit {commit}")


def main() -> None:
    tests = _load_template_tests()

    class _StubAgent:
        async def stream_async(self, _prompt):
            for event in tests._STRANDS_EVENTS:
                yield event

    _write(
        "strands_agentcore_invoke.sse",
        [
            "A SUCCESSFUL tool-using run: the handler drained over the template's own",
            "_STRANDS_EVENTS stub (tests/test_agent.py) and SSE-encoded chunk by chunk exactly as",
            "AgentCore's _convert_to_sse does. Content chunks, then the terminal text-bearing",
            "message. The textless toolUse/toolResult messages and the {'result': AgentResult}",
            "tail are absent because the handler does not forward them.",
        ],
        _encode_sse(_drain(_StubAgent())),
    )

    class _ThrottledAfterToolAgent:
        # The stub from tests/test_agent.py's test_handler_does_not_forward_textless_tool_messages
        # (a completed tool cycle, then a mid-run Bedrock throttle). The messages come from the
        # imported module; only this 3-line shape is restated, because it is function-local there.
        async def stream_async(self, _prompt):
            yield {"message": tests._TOOL_USE_MESSAGE}
            yield {"message": tests._TOOL_RESULT_MESSAGE}
            raise RuntimeError("ThrottlingException: rate exceeded")

    _write(
        "strands_agentcore_invoke_throttled.sse",
        [
            "A run that completes a tool cycle and THEN fails (Bedrock throttling mid-run).",
            "There is NO terminal message: the textless toolUse/toolResult messages were filtered",
            "out by the handler, so the only line on the wire is the error event. A parser that",
            "kept textless messages would extract the stale toolResult blob and hide this error.",
        ],
        _encode_sse(_drain(_ThrottledAfterToolAgent())),
    )


if __name__ == "__main__":
    main()
