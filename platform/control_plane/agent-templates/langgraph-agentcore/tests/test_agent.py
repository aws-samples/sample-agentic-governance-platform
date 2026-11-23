import base64
import json

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import src.main as main_module
from src.config import Config
from src.main import _answer_text, get_current_datetime, handler


def test_datetime_tool_is_utc_string():
    # `@tool` wraps the function, so call it through the tool interface CI will actually exercise.
    assert get_current_datetime.invoke({}).endswith("UTC")


def test_config_defaults(monkeypatch):
    monkeypatch.delenv("MODEL_ID", raising=False)
    monkeypatch.delenv("AWS_REGION", raising=False)
    cfg = Config()
    assert cfg.MODEL_ID == "us.anthropic.claude-sonnet-4-6"
    assert cfg.LOG_LEVEL == "INFO"


def test_config_reads_env(monkeypatch):
    monkeypatch.setenv("MODEL_ID", "custom-model")
    assert Config().MODEL_ID == "custom-model"


# --- response contract ----------------------------------------------------------------------
# The platform's buffering client reads ONE key off this handler's response: `result`. These tests
# pin that shape, because every way of getting it wrong still returns HTTP 200 — a renamed key or
# a list-valued `result` makes the governance UI render a JSON blob where the answer should be,
# with nothing in the logs to say why.


class _StubMessage:
    def __init__(self, content):
        self.content = content


class _StubGraph:
    """Stands in for the compiled graph: records the state it was invoked with, returns a
    terminal message. Substituted for `_graph` so no Bedrock call is made."""

    def __init__(self, content="The time is now."):
        self._content = content
        self.invoked_with = None

    def invoke(self, state):
        self.invoked_with = state
        return {"messages": [_StubMessage(self._content)]}


def _with_graph(monkeypatch, graph):
    monkeypatch.setattr(main_module, "_graph", graph)
    return graph


def test_handler_returns_result_string(monkeypatch):
    _with_graph(monkeypatch, _StubGraph("42"))
    assert handler({"prompt": "what is six times seven?"}) == {"result": "42", "tool_calls": []}


def test_handler_passes_prompt_and_system_prompt_into_the_graph(monkeypatch):
    graph = _with_graph(monkeypatch, _StubGraph())
    handler({"prompt": "hello"})
    roles = [role for role, _ in graph.invoked_with["messages"]]
    assert roles == ["system", "human"]
    assert graph.invoked_with["messages"][-1][1] == "hello"


def test_handler_requires_prompt(monkeypatch):
    """No prompt must not reach the model at all — and must still answer in the result shape."""
    graph = _with_graph(monkeypatch, _StubGraph())
    assert handler({"prompt": ""}) == {"result": "Error: 'prompt' field is required"}
    assert graph.invoked_with is None


def test_handler_returns_errors_in_the_result_shape(monkeypatch):
    """A raise here would become a bodyless 500: the UI shows a failed invocation with no reason."""

    class _BoomGraph:
        def invoke(self, _state):
            raise RuntimeError("ThrottlingException: rate exceeded")

    _with_graph(monkeypatch, _BoomGraph())
    assert handler({"prompt": "hi"}) == {"result": "Error: ThrottlingException: rate exceeded"}


def test_handler_flattens_content_blocks(monkeypatch):
    """Bedrock returns content as blocks once tools are involved — `result` must stay a string."""
    _with_graph(
        monkeypatch,
        _StubGraph([{"type": "text", "text": "The time is "}, {"type": "text", "text": "12:00 UTC"}]),
    )
    assert handler({"prompt": "time?"}) == {"result": "The time is 12:00 UTC", "tool_calls": []}


def test_answer_text_drops_non_text_blocks():
    """Reasoning / tool-use blocks are not the answer and must not be concatenated into it."""
    message = _StubMessage(
        [
            {"type": "reasoning_content", "reasoning_content": {"text": "thinking..."}},
            {"type": "text", "text": "the answer"},
        ]
    )
    assert _answer_text(message) == "the answer"


def test_answer_text_passes_plain_strings_through():
    assert _answer_text(_StubMessage("plain")) == "plain"


# --- telemetry flush ------------------------------------------------------------------------
# The entrypoint force-flushes the TracerProvider THIS MODULE built, because AgentCore freezes the
# microVM after a turn and the BatchSpanProcessor's buffered spans would otherwise be lost. Two
# properties are pinned below, and both were real defects: flushing `get_tracer_provider()` instead
# of our own provider raised AttributeError on EVERY invocation (the global is a
# ProxyTracerProvider, which has no `force_flush`), and a flush fault must never fail the turn.


class _RecordingProvider(TracerProvider):
    """A real provider (the handler opens its root span on it) with a counting flush."""

    def __init__(self, boom=False):
        super().__init__()
        self.flushes = 0
        self._boom = boom

    def force_flush(self):
        self.flushes += 1
        if self._boom:
            raise RuntimeError("exporter unreachable")


def test_handler_flushes_the_module_provider(monkeypatch):
    provider = _RecordingProvider()
    monkeypatch.setattr(main_module, "_tracer_provider", provider)
    _with_graph(monkeypatch, _StubGraph("42"))
    assert handler({"prompt": "hi"}) == {"result": "42", "tool_calls": []}
    assert provider.flushes == 1


def test_handler_flushes_even_when_the_graph_fails(monkeypatch):
    """The flush sits in a `finally` — an error turn's spans are the ones most worth keeping."""

    class _BoomGraph:
        def invoke(self, _state):
            raise RuntimeError("boom")

    provider = _RecordingProvider()
    monkeypatch.setattr(main_module, "_tracer_provider", provider)
    _with_graph(monkeypatch, _BoomGraph())
    assert handler({"prompt": "hi"}) == {"result": "Error: boom"}
    assert provider.flushes == 1


def test_handler_survives_a_failing_flush(monkeypatch):
    """A telemetry fault must never turn a good answer into a failed invocation."""
    provider = _RecordingProvider(boom=True)
    monkeypatch.setattr(main_module, "_tracer_provider", provider)
    _with_graph(monkeypatch, _StubGraph("42"))
    assert handler({"prompt": "hi"}) == {"result": "42", "tool_calls": []}
    assert provider.flushes == 1


def test_handler_does_not_warn_when_telemetry_is_unwired(monkeypatch, caplog):
    """`_tracer_provider is None` is the UNCONFIGURED path — the common local/dev case.

    Nothing to flush and nothing to warn about. The previous implementation called
    `get_tracer_provider().force_flush()` unconditionally, which on this path reached the no-op
    ProxyTracerProvider (no `force_flush` attribute), raised AttributeError, and logged
    "buffered spans may be lost" on every single invocation — an alarming warning about losing
    spans that were never produced.
    """
    monkeypatch.setattr(main_module, "_tracer_provider", None)
    _with_graph(monkeypatch, _StubGraph("42"))
    with caplog.at_level("WARNING"):
        assert handler({"prompt": "hi"}) == {"result": "42", "tool_calls": []}
    assert "buffered spans may be lost" not in caplog.text


# --- trace user id --------------------------------------------------------------------------
# Langfuse's Traces USER column reads `langfuse.user.id`. The handler opens a root span carrying
# the caller's Entra oid, and the LangChain instrumentation's spans nest under it — otherwise every
# trace from a template agent shows USER "—".


def _bearer(claims: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"Bearer e30.{body}.sig"


class _SpanCheckingGraph(_StubGraph):
    def invoke(self, state):
        self.ran_inside_span = trace.get_current_span().get_span_context().is_valid
        return super().invoke(state)


def _traced(monkeypatch, headers=None):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(main_module, "_tracer_provider", provider)
    monkeypatch.setattr(
        main_module.BedrockAgentCoreContext, "get_request_headers", classmethod(lambda cls: headers)
    )
    return exporter, _with_graph(monkeypatch, _SpanCheckingGraph())


def _user_ids(exporter):
    return [s.attributes.get("langfuse.user.id") for s in exporter.get_finished_spans()]


def test_trace_user_id_from_payload_user_oid(monkeypatch):
    """The platform forwards the authenticated caller's oid as `user_oid`."""
    exporter, graph = _traced(monkeypatch)
    assert handler({"prompt": "hi", "user_oid": "oid-from-platform"}) == {
        "result": "The time is now.",
        "tool_calls": [],
    }
    assert _user_ids(exporter) == ["oid-from-platform"]
    assert graph.ran_inside_span, "the graph's spans must nest under the user-tagged root span"


def test_trace_user_id_prefers_the_inbound_bearer_oid(monkeypatch):
    """The inbound bearer was verified by the runtime's JWT authorizer, so its oid wins."""
    exporter, _ = _traced(monkeypatch, headers={"Authorization": _bearer({"oid": "oid-from-token"})})
    handler({"prompt": "hi", "user_oid": "oid-from-platform"})
    assert _user_ids(exporter) == ["oid-from-token"]


def test_no_caller_oid_still_opens_the_root_span_without_a_user(monkeypatch):
    """No oid anywhere (a local curl): same root span name, so traces stay consistent; no user."""
    exporter, _ = _traced(monkeypatch, headers={"Authorization": "Bearer not-a-jwt"})
    assert handler({"prompt": "hi"}) == {"result": "The time is now.", "tool_calls": []}
    assert [s.name for s in exporter.get_finished_spans()] == ["invoke_agent"]
    assert _user_ids(exporter) == [None]


def test_invalid_user_oid_is_ignored(monkeypatch):
    """A direct caller controls the payload: a non-string or oversized oid is dropped, not sent."""
    exporter, _ = _traced(
        monkeypatch,
    )
    handler({"prompt": "hi", "user_oid": {"not": "a string"}})
    handler({"prompt": "hi", "user_oid": "x" * 129})
    assert _user_ids(exporter) == [None, None]


# --- evaluations: tool calls + CloudWatch traces (E42) --------------------------------------
# The tool checks read `tool_calls` off the response; the judges read the spans the second
# (X-Ray OTLP) processor exports. tests/conftest.py switches that export off at import, so these
# drive `_setup_cloudwatch_traces()` directly.


class _ToolCallingMessage(_StubMessage):
    def __init__(self, content, tool_calls):
        super().__init__(content)
        self.tool_calls = tool_calls


def test_handler_returns_the_tool_calls_of_the_run(monkeypatch):
    class _ToolGraph(_StubGraph):
        def invoke(self, state):
            call = {"name": "get_current_datetime", "args": {}, "id": "t1", "type": "tool_call"}
            return {"messages": [_ToolCallingMessage("", [call]), _StubMessage("It is noon.")]}

    _with_graph(monkeypatch, _ToolGraph())
    assert handler({"prompt": "time?"}) == {
        "result": "It is noon.",
        "tool_calls": [{"name": "get_current_datetime", "args": {}}],
    }


def test_handler_skips_tool_calls_without_a_name(monkeypatch):
    class _ToolGraph(_StubGraph):
        def invoke(self, state):
            calls = [
                {"name": None, "args": {"x": 1}, "id": "t1", "type": "tool_call"},
                {"name": "get_current_datetime", "args": {}, "id": "t2", "type": "tool_call"},
            ]
            return {"messages": [_ToolCallingMessage("", calls), _StubMessage("It is noon.")]}

    _with_graph(monkeypatch, _ToolGraph())
    assert handler({"prompt": "time?"})["tool_calls"] == [{"name": "get_current_datetime", "args": {}}]


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
