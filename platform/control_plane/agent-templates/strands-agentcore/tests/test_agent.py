import asyncio
import json

import src.main as main_module
from src.config import Config
from src.main import calculator, get_current_datetime, handler


def test_calculator_evaluates_safely():
    assert calculator("sqrt(144) + 2 * 3") == "18.0"

def test_calculator_rejects_unsafe():
    assert calculator("__import__('os')").startswith("Error")

def test_datetime_is_utc_string():
    assert get_current_datetime().endswith("UTC")

def test_config_defaults(monkeypatch):
    monkeypatch.delenv("MODEL_ID", raising=False)
    monkeypatch.delenv("AWS_REGION", raising=False)
    cfg = Config()
    assert cfg.MODEL_ID == "us.anthropic.claude-sonnet-4-6"
    assert cfg.LOG_LEVEL == "INFO"

def test_config_reads_env(monkeypatch):
    monkeypatch.setenv("MODEL_ID", "custom-model")
    assert Config().MODEL_ID == "custom-model"


# --- handler event forwarding ---------------------------------------------------------------
# The handler's stream is read by two kinds of client, and the events they need are different:
# a streaming UI consumes the incremental {"type": "content", ...} chunks, while a BUFFERING
# client (the AGP control plane's POST /agents/{id}/invoke) reconstructs the answer from the
# TERMINAL Strands {"message": ...} event by joining its content[].text. These tests pin both
# halves, because dropping the terminal event is a silent failure: the stream still looks fine
# and a buffering client just shows the last token as the whole answer.

# Recorded from the pinned strands version's real agent.stream_async output and trimmed to the
# keys the handler reads (a tool-using run, so there are intermediate `message` events before the
# terminal one). The {"result": AgentResult} tail is represented by the sentinel object below,
# which stands in for its defining property here: not JSON-serializable.
_TOOL_USE_MESSAGE = {
    "role": "assistant",
    "content": [{"toolUse": {"toolUseId": "t1", "name": "calculator", "input": {"expression": "1+1"}}}],
}
_TOOL_RESULT_MESSAGE = {
    "role": "user",
    "content": [{"toolResult": {"toolUseId": "t1", "status": "success", "content": [{"text": "2"}]}}],
}
_TERMINAL_MESSAGE = {"role": "assistant", "content": [{"text": "The answer is 2 😊"}]}


class _NotSerializable:
    """Stands in for strands' AgentResult, which json.dumps cannot encode."""


_STRANDS_EVENTS = [
    {"init_event_loop": True},
    {"event": {"messageStart": {"role": "assistant"}}},
    {"message": _TOOL_USE_MESSAGE},
    {"message": _TOOL_RESULT_MESSAGE},
    {"data": "The answer is ", "delta": {"text": "The answer is "}},
    {"data": "2", "delta": {"text": "2"}},
    {"data": " 😊", "delta": {"text": " 😊"}},
    {"event": {"messageStop": {"stopReason": "end_turn"}}},
    {"message": _TERMINAL_MESSAGE},
    {"result": _NotSerializable()},
]


def _drain_handler(monkeypatch, events, prompt="math please"):
    class _StubAgent:
        async def stream_async(self, _prompt):
            for event in events:
                yield event

    monkeypatch.setattr(main_module, "agent", _StubAgent())

    async def _collect():
        return [chunk async for chunk in handler({"prompt": prompt})]

    return asyncio.run(_collect())


def test_handler_streams_content_chunks(monkeypatch):
    chunks = _drain_handler(monkeypatch, _STRANDS_EVENTS)
    assert [c["data"] for c in chunks if c.get("type") == "content"] == [
        "The answer is ",
        "2",
        " 😊",
    ]


def test_handler_forwards_terminal_message_last(monkeypatch):
    """A buffering client keys on the LAST message-shaped event — it must be the terminal one."""
    chunks = _drain_handler(monkeypatch, _STRANDS_EVENTS)
    messages = [c["message"] for c in chunks if "message" in c]
    assert messages[-1] == _TERMINAL_MESSAGE
    # And the answer it reconstructs is the WHOLE answer, not just the last token — the defect
    # this guards against showed `{"type": "content", "data": "😊"}` in the governance UI.
    joined = "".join(part["text"] for part in messages[-1]["content"] if "text" in part)
    assert joined == "The answer is 2 😊"


def test_handler_yields_only_json_serializable_events(monkeypatch):
    """Everything forwarded is SSE-encoded, so a non-serializable yield would break the stream.

    In particular the {"result": AgentResult} event strands yields after the terminal message is
    deliberately NOT forwarded.
    """
    chunks = _drain_handler(monkeypatch, _STRANDS_EVENTS)
    for chunk in chunks:
        json.dumps(chunk)  # raises TypeError if anything non-serializable slipped through


def test_handler_requires_prompt(monkeypatch):
    chunks = _drain_handler(monkeypatch, _STRANDS_EVENTS, prompt="")
    assert chunks == [{"type": "error", "data": "'prompt' field is required"}]


def _buffered_client_view(chunks):
    """Mirror how a buffering client reads this stream: last message-shaped event wins.

    This is the extraction rule of the AGP control plane's ``_extract_sse_text`` reduced to the
    part these tests pin. Reproduced rather than imported on purpose — this template ships as a
    standalone repository, so a test here cannot reach the platform's source.

    It is a CONVENIENCE, not the evidence. The real cross-check lives on the platform side: a body
    captured from this handler, SSE-encoded, asserted by the actual parser
    (``BE/tests/test_invoke_route.py`` + ``BE/tests/fixtures/strands_agentcore_invoke*.sse``). This
    reproduction only keeps the template standalone-testable; it cannot detect the platform
    changing its rule, which is why the fence test exists.
    """
    messages = [c["message"] for c in chunks if "message" in c]
    if messages:
        texts = [p["text"] for p in messages[-1].get("content", []) if isinstance(p, dict) and "text" in p]
        if texts:
            return "".join(texts)
        return messages[-1]
    return chunks[-1] if chunks else None


def test_handler_does_not_forward_textless_tool_messages(monkeypatch):
    """A tool cycle that then FAILS must surface the error, not a stale toolResult blob.

    The tool-call and tool-result messages carry no `text`, and a buffering client keeps the last
    message-shaped event whether or not it has text. If those were forwarded, this stream — a
    completed tool cycle followed by a mid-run failure, e.g. Bedrock throttling — would extract
    the toolResult and render it as the answer, hiding the error entirely.
    """

    class _ThrottledAfterToolAgent:
        async def stream_async(self, _prompt):
            yield {"message": _TOOL_USE_MESSAGE}
            yield {"message": _TOOL_RESULT_MESSAGE}
            raise RuntimeError("ThrottlingException: rate exceeded")

    monkeypatch.setattr(main_module, "agent", _ThrottledAfterToolAgent())

    async def _collect():
        return [chunk async for chunk in handler({"prompt": "math please"})]

    chunks = asyncio.run(_collect())
    assert not any("message" in c for c in chunks), "textless tool messages must not be forwarded"
    assert chunks == [{"type": "error", "data": "ThrottlingException: rate exceeded"}]
    assert _buffered_client_view(chunks) == {
        "type": "error",
        "data": "ThrottlingException: rate exceeded",
    }


def test_buffered_client_sees_full_answer_on_tool_cycle(monkeypatch):
    """The filter above must not cost the success case its answer."""
    chunks = _drain_handler(monkeypatch, _STRANDS_EVENTS)
    assert _buffered_client_view(chunks) == "The answer is 2 😊"


def test_handler_reports_stream_errors(monkeypatch):
    class _BoomAgent:
        async def stream_async(self, _prompt):
            yield {"data": "partial"}
            raise RuntimeError("model exploded")

    monkeypatch.setattr(main_module, "agent", _BoomAgent())

    async def _collect():
        return [chunk async for chunk in handler({"prompt": "hi"})]

    chunks = asyncio.run(_collect())
    assert chunks == [
        {"type": "content", "data": "partial"},
        {"type": "error", "data": "model exploded"},
    ]
