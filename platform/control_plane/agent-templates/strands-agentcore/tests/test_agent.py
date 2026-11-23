import asyncio
import base64
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


# --- telemetry flush ------------------------------------------------------------------------
# The entrypoint force-flushes the TracerProvider STRANDS WIRED (captured from the
# `StrandsTelemetry` instance, which owns it), because AgentCore freezes the microVM after a turn
# and the BatchSpanProcessor's buffered spans would otherwise be lost. Two properties are pinned
# below, and both were real defects: flushing `get_tracer_provider()` instead raised AttributeError
# on EVERY invocation of an unwired agent (the global is a `ProxyTracerProvider`, which has no
# `force_flush` attribute), and a flush fault must never fail the turn.


class _RecordingProvider:
    def __init__(self, boom=False):
        self.flushes = 0
        self._boom = boom

    def force_flush(self):
        self.flushes += 1
        if self._boom:
            raise RuntimeError("exporter unreachable")


def test_handler_flushes_the_provider_strands_wired(monkeypatch):
    """The wired path: our captured provider is the one flushed, exactly once per turn."""
    provider = _RecordingProvider()
    monkeypatch.setattr(main_module, "_tracer_provider", provider)

    chunks = _drain_handler(monkeypatch, _STRANDS_EVENTS)

    assert [c["message"] for c in chunks if "message" in c][-1] == _TERMINAL_MESSAGE
    assert provider.flushes == 1


def test_handler_does_not_warn_when_telemetry_is_unwired(monkeypatch, caplog):
    """`_tracer_provider is None` is the UNCONFIGURED path — the common local/dev case.

    Nothing to flush and nothing to warn about. The previous implementation called
    `get_tracer_provider().force_flush()` unconditionally, which on this path reached the no-op
    `ProxyTracerProvider` (no `force_flush` attribute), raised AttributeError, and logged
    "buffered spans may be lost" on every single invocation — an alarming warning about losing
    spans that were never produced.

    The real `ProxyTracerProvider` is installed rather than a stub, and deliberately so: a stub
    that HAS `force_flush` cannot reproduce the one property that mattered. It is also set
    explicitly rather than assumed, because `set_tracer_provider` is set-once and process-wide, so
    another test could otherwise leave a real provider behind and make this pass by accident.
    """
    from opentelemetry import trace as trace_api
    from opentelemetry.trace import ProxyTracerProvider

    monkeypatch.setattr(main_module, "_tracer_provider", None)
    monkeypatch.setattr(trace_api, "get_tracer_provider", ProxyTracerProvider)

    with caplog.at_level("WARNING"):
        chunks = _drain_handler(monkeypatch, _STRANDS_EVENTS)

    assert [c["message"] for c in chunks if "message" in c][-1] == _TERMINAL_MESSAGE
    assert "buffered spans may be lost" not in caplog.text


def test_handler_survives_a_failing_flush(monkeypatch, caplog):
    """A flush fault is logged and swallowed — losing spans must never cost the turn."""
    monkeypatch.setattr(main_module, "_tracer_provider", _RecordingProvider(boom=True))

    with caplog.at_level("WARNING"):
        chunks = _drain_handler(monkeypatch, _STRANDS_EVENTS)

    assert [c["message"] for c in chunks if "message" in c][-1] == _TERMINAL_MESSAGE
    assert "buffered spans may be lost" in caplog.text


# --- trace user id --------------------------------------------------------------------------
# Langfuse's Traces USER column reads `langfuse.user.id`. Strands applies an Agent's
# `trace_attributes` to every span it creates, so the handler sets the caller's Entra oid there —
# otherwise every trace from a template agent shows USER "—". These drive a REAL agent (only the
# model's network call is stubbed) and read what strands hands its tracer.


def _bearer(claims: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"Bearer e30.{body}.sig"


async def _one_word_stream(_self, _messages, tool_specs=None, system_prompt=None, **_kwargs):
    yield {"messageStart": {"role": "assistant"}}
    yield {"contentBlockDelta": {"delta": {"text": "hi"}}}
    yield {"contentBlockStop": {}}
    yield {"messageStop": {"stopReason": "end_turn"}}


def _real_agent(monkeypatch, headers=None):
    """A real module agent; returns the trace attributes strands passes to each agent span."""
    monkeypatch.setattr(main_module.BedrockModel, "stream", _one_word_stream)
    real = main_module._build_agent()
    spans: list[dict] = []
    start = real.tracer.start_agent_span

    def _spy_start(**kwargs):
        spans.append(dict(kwargs["custom_trace_attributes"]))
        return start(**kwargs)

    monkeypatch.setattr(real.tracer, "start_agent_span", _spy_start)
    monkeypatch.setattr(main_module, "agent", real)
    monkeypatch.setattr(main_module, "_mcp_tools", lambda _stack, _refused: [])
    monkeypatch.setattr(
        main_module.BedrockAgentCoreContext, "get_request_headers", classmethod(lambda cls: headers)
    )
    return spans


def _run(payload):
    async def _collect():
        return [chunk async for chunk in handler(payload)]

    return asyncio.run(_collect())


def test_build_agent_hands_trace_attributes_to_the_real_agent():
    built = main_module._build_agent(trace_attributes={"langfuse.user.id": "x"})
    assert built.trace_attributes["langfuse.user.id"] == "x"


def test_trace_user_id_from_payload_user_oid(monkeypatch):
    """The platform forwards the authenticated caller's oid as `user_oid`."""
    spans = _real_agent(monkeypatch)
    _run({"prompt": "hi", "user_oid": "oid-from-platform"})
    assert spans == [{"langfuse.user.id": "oid-from-platform"}]


def test_trace_user_id_prefers_the_inbound_bearer_oid(monkeypatch):
    """The inbound bearer was verified by the runtime's JWT authorizer, so its oid wins."""
    spans = _real_agent(monkeypatch, headers={"Authorization": _bearer({"oid": "oid-from-token"})})
    _run({"prompt": "hi", "user_oid": "oid-from-platform"})
    assert spans == [{"langfuse.user.id": "oid-from-token"}]


def test_invalid_user_oid_is_ignored(monkeypatch):
    """A direct caller controls the payload: a non-string or oversized oid is dropped, not sent."""
    spans = _real_agent(monkeypatch)
    _run({"prompt": "hi", "user_oid": {"not": "a string"}})
    _run({"prompt": "hi", "user_oid": "x" * 129})
    assert spans == [{}, {}]


def test_two_invokes_reuse_one_agent_and_one_model(monkeypatch):
    """Per-caller tagging must not cost a new agent or Bedrock client per call — and a caller's
    id must not linger on the next, anonymous call."""
    spans = _real_agent(monkeypatch)
    module_agent = main_module.agent
    model = module_agent.model

    def _no_new_model(*_args, **_kwargs):
        raise AssertionError("an invoke must not construct a new BedrockModel")

    def _no_rebuild(*_args, **_kwargs):
        raise AssertionError("an invoke without MCP tools must not rebuild the agent")

    monkeypatch.setattr(main_module, "BedrockModel", _no_new_model)
    monkeypatch.setattr(main_module, "_build_agent", _no_rebuild)

    chunks = _run({"prompt": "one", "user_oid": "oid-a"})
    _run({"prompt": "two"})

    assert main_module.agent is module_agent and module_agent.model is model
    assert spans == [{"langfuse.user.id": "oid-a"}, {}]
    assert [c["data"] for c in chunks if c.get("type") == "content"] == ["hi"]


# --- evaluations: tool calls + CloudWatch traces (E42) --------------------------------------
# The tool checks read the `tool_calls` event the handler yields last; the judges read the spans
# the second (X-Ray OTLP) processor exports. tests/conftest.py switches that export off at import,
# so these drive `_setup_cloudwatch_traces()` directly.


def test_handler_yields_tool_calls_last_on_success(monkeypatch):
    chunks = _drain_handler(monkeypatch, _STRANDS_EVENTS)
    assert chunks[-1] == {"tool_calls": [{"name": "calculator", "args": {"expression": "1+1"}}]}
    assert _buffered_client_view(chunks) == "The answer is 2 😊"  # still lands on the answer


def test_handler_yields_empty_tool_calls_without_tools(monkeypatch):
    chunks = _drain_handler(monkeypatch, [{"data": "hi"}, {"message": _TERMINAL_MESSAGE}])
    assert chunks[-1] == {"tool_calls": []}


def test_handler_skips_tool_calls_without_a_name(monkeypatch):
    nameless = {
        "role": "assistant",
        "content": [
            {"toolUse": {"toolUseId": "t1", "input": {"x": 1}}},
            {"toolUse": {"toolUseId": "t2", "name": "calculator", "input": {"expression": "2"}}},
        ],
    }
    chunks = _drain_handler(monkeypatch, [{"message": nameless}, {"message": _TERMINAL_MESSAGE}])
    assert chunks[-1] == {"tool_calls": [{"name": "calculator", "args": {"expression": "2"}}]}


def _recording_session(monkeypatch):
    """Capture what the SigV4 session hands requests, and answer 200 without a network call."""
    import requests
    from requests.sessions import merge_setting
    from requests.structures import CaseInsensitiveDict

    sent = []

    def _fake_request(self, method, url, *args, headers=None, **kwargs):
        # requests' own merge: a None on the call removes that session header.
        merged = merge_setting(headers, self.headers, dict_class=CaseInsensitiveDict)
        sent.append({"url": url, "headers": dict(merged)})
        resp = requests.Response()
        resp.status_code = 200
        return resp

    monkeypatch.setattr(requests.Session, "request", _fake_request)
    return sent


def test_xray_exporter_signs_with_sigv4_and_drops_the_langfuse_auth(monkeypatch):
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    for var in ("AWS_PROFILE", "AWS_SESSION_TOKEN"):  # env keys only, never a real profile
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "not-a-real-secret")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "Authorization=Basic langfuse-key")
    sent = _recording_session(monkeypatch)
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(main_module._xray_span_exporter("eu-west-1")))

    with provider.get_tracer("t").start_as_current_span("s"):
        pass

    assert sent[0]["url"] == "https://xray.eu-west-1.amazonaws.com/v1/traces"
    auth = [v for k, v in sent[0]["headers"].items() if k.lower() == "authorization"]
    assert auth == [auth[0]] and auth[0].startswith("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/")
    assert "/eu-west-1/xray/" in auth[0]


def _export_spans(count=1, region="eu-west-1"):
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(main_module._xray_span_exporter(region)))
    for _ in range(count):  # one exporter, so one session: each span is its own POST
        with provider.get_tracer("t").start_as_current_span("s"):
            pass


def test_xray_exporter_sends_aws_only_an_allowlist_of_headers(monkeypatch):
    for var in ("AWS_PROFILE", "AWS_SESSION_TOKEN"):  # env keys only, never a real profile
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "not-a-real-secret")
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_HEADERS", "Authorization=Basic langfuse-key,x-api-key=other-vendor-key"
    )
    sent = _recording_session(monkeypatch)

    _export_spans()

    names = {k.lower() for k, v in sent[0]["headers"].items() if v is not None}
    assert names <= {"content-type", "content-encoding", "user-agent", "authorization", "x-amz-date"}
    assert {"content-type", "authorization", "x-amz-date"} <= names
    assert not any("langfuse-key" in v or "other-vendor-key" in v for v in sent[0]["headers"].values())


def test_xray_exporter_signs_with_credentials_frozen_once_per_request(monkeypatch):
    import botocore.session
    from botocore.credentials import ReadOnlyCredentials

    class _RefreshingCredentials:
        frozen = 0

        def get_frozen_credentials(self):
            type(self).frozen += 1
            return ReadOnlyCredentials("AKIDFROZEN", "not-a-real-secret", "a-session-token")

        def __getattr__(self, name):  # access_key, secret_key, token: never read off the live object
            raise AssertionError(f"read {name} off refreshable credentials")

    monkeypatch.setattr(botocore.session.Session, "get_credentials", lambda self: _RefreshingCredentials())
    sent = _recording_session(monkeypatch)

    _export_spans(2)

    assert len(sent) == 2 and _RefreshingCredentials.frozen == 2
    for request in sent:
        headers = {k.lower(): v for k, v in request["headers"].items()}
        assert headers["authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKIDFROZEN/")
        assert headers["x-amz-security-token"] == "a-session-token"


def test_cloudwatch_traces_add_a_second_processor_with_the_session_id(monkeypatch):
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    langfuse, cloudwatch = InMemorySpanExporter(), InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(langfuse))
    monkeypatch.setattr(main_module, "_tracer_provider", provider)
    monkeypatch.setattr(main_module, "_xray_span_exporter", lambda _region: cloudwatch)
    monkeypatch.setattr(
        main_module.BedrockAgentCoreContext, "get_session_id", classmethod(lambda cls: "sess-1")
    )
    monkeypatch.delenv("AGP_CLOUDWATCH_TRACES", raising=False)

    main_module._setup_cloudwatch_traces()
    with provider.get_tracer("t").start_as_current_span("s"):
        pass
    provider.force_flush()

    assert [s.name for s in langfuse.get_finished_spans()] == ["s"]  # Langfuse keeps its spans
    assert [s.attributes.get("session.id") for s in cloudwatch.get_finished_spans()] == ["sess-1"]


def test_cloudwatch_traces_opt_out(monkeypatch):
    def _never(_region):
        raise AssertionError("the exporter must not be built when opted out")

    monkeypatch.setattr(main_module, "_xray_span_exporter", _never)
    monkeypatch.setenv("AGP_CLOUDWATCH_TRACES", "false")
    main_module._setup_cloudwatch_traces()


def test_cloudwatch_traces_failure_is_logged_not_raised(monkeypatch, caplog):
    def _boom(_region):
        raise RuntimeError("no AWS credentials")

    monkeypatch.setattr(main_module, "_xray_span_exporter", _boom)
    monkeypatch.delenv("AGP_CLOUDWATCH_TRACES", raising=False)
    with caplog.at_level("WARNING"):
        main_module._setup_cloudwatch_traces()
    assert "CloudWatch traces disabled" in caplog.text
