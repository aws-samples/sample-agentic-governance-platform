"""Unit tests for the CrewAI scaffold: config, telemetry, the crew builder, and the contract.

No test here calls Bedrock or Secrets Manager. `build_crew` is exercised for its SHAPE (a real
`Crew` object, constructed offline — CrewAI's `LLM` resolves no credentials at construction), the
handler is driven with `build_crew` stubbed, and the Langfuse setup is driven with `boto3` stubbed.
The whole file runs with no AWS credentials and no network.
"""

import base64
import json
import os
import pathlib

import pytest
from opentelemetry import trace as trace_api
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import src.main as main_module
from src.config import Config
from src.main import _setup_langfuse_telemetry, build_crew, handler

# The OTEL variables the telemetry block writes. Named once because two fixtures need the list.
_OTEL_VARS = (
    "DISABLE_ADOT_OBSERVABILITY",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_HEADERS",
    "OTEL_EXPORTER_OTLP_PROTOCOL",
    "OTEL_SERVICE_NAME",
)


@pytest.fixture
def clean_otel_env(monkeypatch):
    """Clear the OTEL vars and the Langfuse config so each telemetry test starts unconfigured.

    Import of `src.main` already ran `_setup_langfuse_telemetry()` once, and several tests below
    assert on the ABSENCE of these variables — without this the suite's result would depend on the
    developer's own environment, and on test order.
    """
    for var in _OTEL_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(main_module.config, "LANGFUSE_HOST", "")
    monkeypatch.setattr(main_module.config, "LANGFUSE_SECRET_NAME", "")
    return monkeypatch


# --- config ---------------------------------------------------------------------------------


def test_config_defaults(monkeypatch):
    monkeypatch.delenv("MODEL_ID", raising=False)
    monkeypatch.delenv("AWS_REGION", raising=False)
    cfg = Config()
    assert cfg.MODEL_ID == "us.anthropic.claude-sonnet-4-6"
    assert cfg.AWS_REGION == "us-east-1"
    assert cfg.LOG_LEVEL == "INFO"


def test_config_reads_env(monkeypatch):
    monkeypatch.setenv("MODEL_ID", "custom-model")
    assert Config().MODEL_ID == "custom-model"


def test_crewai_model_adds_litellm_provider_prefix(monkeypatch):
    """CrewAI routes through LiteLLM, which needs `<provider>/<model>` — a bare id fails at CALL
    time, long after construction, so pin the prefixing here."""
    monkeypatch.setenv("MODEL_ID", "us.anthropic.claude-sonnet-4-6")
    assert Config().CREWAI_MODEL == "bedrock/us.anthropic.claude-sonnet-4-6"


def test_crewai_model_prefixing_is_idempotent(monkeypatch):
    """An operator who sets an already-prefixed MODEL_ID must not get `bedrock/bedrock/...`."""
    monkeypatch.setenv("MODEL_ID", "bedrock/us.anthropic.claude-sonnet-4-6")
    assert Config().CREWAI_MODEL == "bedrock/us.anthropic.claude-sonnet-4-6"


def test_config_langfuse_defaults_to_unset(monkeypatch):
    """Unset is the DEFAULT, and it must be falsy — that is what selects the no-op path."""
    monkeypatch.delenv("LANGFUSE_HOST", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_NAME", raising=False)
    cfg = Config()
    assert cfg.LANGFUSE_HOST == ""
    assert cfg.LANGFUSE_SECRET_NAME == ""


# --- Langfuse telemetry: the graceful-degrade paths -----------------------------------------
# The whole block runs at MODULE IMPORT, so a fault there does not cost a span — it crash-loops
# the runtime before `app.run()`. An agent with no traces is degraded; an agent that will not
# start is down. Every one of these tests therefore asserts the same two things: no exception
# escapes, and no OTEL env is written (a half-configured exporter is its own failure mode).


def _assert_no_otel_env_written():
    import os

    for var in _OTEL_VARS:
        assert var not in os.environ, f"{var} was set on a path where telemetry must NOT be wired"


def test_telemetry_is_a_noop_when_unconfigured(clean_otel_env):
    """The DEFAULT deployment shape: no Langfuse env injected ⇒ no telemetry, no error.

    This is the path every agent takes before the platform provisions its Langfuse project, and a
    local `python -m src.main` takes it too.
    """
    _setup_langfuse_telemetry()  # must not raise
    _assert_no_otel_env_written()


def test_telemetry_is_a_noop_when_only_host_is_set(clean_otel_env):
    """Half-configured is treated as unconfigured — the secret NAME is what makes the key readable."""
    clean_otel_env.setattr(main_module.config, "LANGFUSE_HOST", "https://langfuse.example.com")
    _setup_langfuse_telemetry()
    _assert_no_otel_env_written()


def test_telemetry_is_a_noop_when_only_secret_name_is_set(clean_otel_env):
    clean_otel_env.setattr(main_module.config, "LANGFUSE_SECRET_NAME", "agp/langfuse/agent-1")
    _setup_langfuse_telemetry()
    _assert_no_otel_env_written()


def test_telemetry_degrades_when_the_secret_is_unreadable(clean_otel_env):
    """A missing secret / denied read must not take the agent down with it.

    Realistic: the runtime can start before the platform has provisioned the secret, or with an
    execution role that cannot read it.
    """
    clean_otel_env.setattr(main_module.config, "LANGFUSE_HOST", "https://langfuse.example.com")
    clean_otel_env.setattr(main_module.config, "LANGFUSE_SECRET_NAME", "agp/langfuse/agent-1")

    def _boom(*_args, **_kwargs):
        raise RuntimeError("AccessDeniedException: not authorized to perform secretsmanager:GetSecretValue")

    clean_otel_env.setattr(main_module.boto3, "client", _boom)

    _setup_langfuse_telemetry()  # must not raise
    _assert_no_otel_env_written()


def _stub_secretsmanager(monkeypatch, secret_payload: str):
    """Point `boto3.client("secretsmanager")` at a stub returning `secret_payload`."""

    class _StubSM:
        # `SecretId` is boto3's own parameter name and the caller passes it as a keyword, so the
        # capital is required here. (No `noqa`: pep8-naming `N` is not in this template's select.)
        def get_secret_value(self, SecretId):
            return {"SecretString": secret_payload}

    monkeypatch.setattr(main_module.boto3, "client", lambda *_a, **_kw: _StubSM())


def test_telemetry_degrades_when_the_secret_lacks_the_key_pair(clean_otel_env):
    """A secret that exists but holds the wrong shape is still unusable — degrade, don't guess."""
    clean_otel_env.setattr(main_module.config, "LANGFUSE_HOST", "https://langfuse.example.com")
    clean_otel_env.setattr(main_module.config, "LANGFUSE_SECRET_NAME", "agp/langfuse/agent-1")
    _stub_secretsmanager(clean_otel_env, '{"username": "wrong-shape"}')

    _setup_langfuse_telemetry()
    _assert_no_otel_env_written()


# The configured path can only be tested in a FRESH interpreter. The defect this guards is an
# import-ORDER one, and by the time this test module runs, `src.main` (and therefore `crewai`) is
# already imported — re-running the setup in-process would import nothing and prove nothing. So the
# check runs in a subprocess that imports the module for the first time with Langfuse configured.
_CONFIGURED_PATH_PROBE = """
import os
os.environ["LANGFUSE_HOST"] = "https://langfuse.example.com"
os.environ["LANGFUSE_SECRET_NAME"] = "agp/langfuse/agent-1"
os.environ.pop("CREWAI_DISABLE_TELEMETRY", None)

import boto3


class _StubSM:
    def get_secret_value(self, SecretId):
        return {"SecretString": '{"public_key": "pk-lf-1", "secret_key": "sk-lf-2"}'}


# Patch the ATTRIBUTE, not the module: bedrock_agentcore needs the real boto3.Session.
boto3.client = lambda *a, **kw: _StubSM()

import src.main

from crewai.telemetry import Telemetry

# `_tracer_provider`, NOT the OTEL env var. The env is written BEFORE the try block that builds
# the provider and instruments, so asserting on it would pass even if the instrumentor import
# failed — a false green on exactly the half this test exists to prove.
print("WIRED", src.main._tracer_provider is not None)
print("PROVIDER", type(src.main._tracer_provider).__name__)
print("READY", Telemetry().ready)
"""


def test_vendor_telemetry_stays_inert_on_the_langfuse_configured_path(tmp_path):
    """The production path, in a fresh interpreter: Langfuse wired AND vendor telemetry inert.

    Both halves matter and they are in tension, which is why this is one test: the Langfuse
    instrumentor is itself what imports `crewai`, so "traces work" and "the vendor export is off"
    were briefly mutually exclusive here. The regression was invisible to an env-var assertion and
    fired only when a Langfuse secret was present — production only.
    """
    import subprocess
    import sys

    probe = tmp_path / "configured_path_probe.py"
    probe.write_text(_CONFIGURED_PATH_PROBE)

    repo_root = pathlib.Path(__file__).resolve().parents[1]
    result = subprocess.run(  # noqa: S603 — fixed argv, no shell, our own interpreter
        [sys.executable, str(probe)],
        cwd=repo_root,
        env={**os.environ, "PYTHONPATH": str(repo_root)},
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, f"probe failed:\n{result.stdout}\n{result.stderr}"
    assert "WIRED True" in result.stdout, (
        "Langfuse telemetry did not wire on the configured path — `_tracer_provider` is still "
        f"None, so the provider/instrumentor step failed:\n{result.stdout}"
    )
    # A real SDK provider, not the no-op Proxy: the flush in the entrypoint depends on it.
    assert "PROVIDER TracerProvider" in result.stdout, (
        f"expected an SDK TracerProvider on the configured path:\n{result.stdout}"
    )
    assert "READY False" in result.stdout, (
        "crewai's vendor telemetry initialised on the Langfuse-configured path. CREWAI_DISABLE_"
        "TELEMETRY must be set BEFORE _setup_langfuse_telemetry() runs, because the instrumentor "
        f"imports crewai itself.\n{result.stdout}"
    )


def test_telemetry_wires_the_exporter_when_fully_configured(clean_otel_env):
    """The configured path: OTEL env is written from the secret, and never the raw key values."""
    import base64
    import os

    clean_otel_env.setattr(main_module.config, "LANGFUSE_HOST", "https://langfuse.example.com/")
    clean_otel_env.setattr(main_module.config, "LANGFUSE_SECRET_NAME", "agp/langfuse/agent-1")
    _stub_secretsmanager(clean_otel_env, '{"public_key": "pk-lf-123", "secret_key": "sk-lf-456"}')

    _setup_langfuse_telemetry()

    # ADOT owns the global TracerProvider first (set-once); disabling it is what lets the Langfuse
    # exporter win, and it is set ONLY on this path.
    assert os.environ["DISABLE_ADOT_OBSERVABILITY"] == "true"
    # BASE endpoint form — the HTTP exporter appends /v1/traces itself. A trailing slash on the
    # host must not produce a double slash here.
    assert os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] == "https://langfuse.example.com/api/public/otel"
    # Langfuse does not support gRPC.
    assert os.environ["OTEL_EXPORTER_OTLP_PROTOCOL"] == "http/protobuf"

    expected_auth = base64.b64encode(b"pk-lf-123:sk-lf-456").decode()
    assert os.environ["OTEL_EXPORTER_OTLP_HEADERS"] == f"Authorization=Basic {expected_auth}"
    # The base64 auth IS the secret, so the raw key values must never appear on their own.
    assert "sk-lf-456" not in os.environ["OTEL_EXPORTER_OTLP_HEADERS"]


def test_crewai_telemetry_env_flag_is_set_by_default():
    """CrewAI's phone-home to CrewAI Inc is opt-OUT, and the flag is read at package import.

    This asserts the INPUT only. It is necessary but NOT sufficient — see the test below, which
    asserts the resulting behaviour. Keep both: this one localises a regression to the assignment,
    the next one catches the case where the assignment is present but too late to matter.
    """
    import os

    assert os.environ["CREWAI_DISABLE_TELEMETRY"] == "true"


def test_crewai_vendor_telemetry_is_actually_inert():
    """The env flag must have taken EFFECT, not merely been set.

    This is the test that catches an ordering regression, and it exists because the env assertion
    above could not. The `setdefault` once sat BELOW the Langfuse block, whose instrumentor imports
    `crewai` itself — so by the time the flag was assigned, `crewai.telemetry` had already read an
    unset value and initialised: the variable said "true" while telemetry was live, signal handlers
    were hijacked, and CrewAI's own span processor had attached to the platform's Langfuse provider.
    Worse, that only happened on the Langfuse-CONFIGURED path, i.e. exclusively in production.

    `Telemetry().ready` is the observable state, so that is what is asserted. A customer's agent
    must not emit usage data to a third party outside the platform's trust chain.
    """
    from crewai.telemetry import Telemetry

    assert Telemetry().ready is False, (
        "crewai's vendor telemetry initialised despite CREWAI_DISABLE_TELEMETRY — the flag is "
        "almost certainly being set AFTER something imports crewai (the Langfuse instrumentor "
        "does). It must be set before any such import, near the top of src/main.py."
    )


# --- crew construction ----------------------------------------------------------------------


def test_build_crew_wires_one_agent_and_the_prompt_as_the_task():
    """The starter is a SINGLE-crew scaffold, and the prompt is the task's description."""
    crew = build_crew("what is 2 + 2?")

    assert len(crew.agents) == 1
    assert len(crew.tasks) == 1
    assert crew.tasks[0].description == "what is 2 + 2?"
    assert crew.tasks[0].agent is crew.agents[0]
    # Delegation with a single agent can only fail (there is nobody to delegate to) while still
    # spending tokens on the attempt.
    assert crew.agents[0].allow_delegation is False


def test_build_crew_is_per_invocation():
    """Two calls must not share a Crew.

    A CrewAI `Task` CARRIES the request, so a reused module-level crew would answer the second
    caller with the first caller's task — wrong, and silently so. Distinct objects is the property
    that rules that out.
    """
    first = build_crew("first question")
    second = build_crew("second question")

    assert first is not second
    assert first.tasks[0].description == "first question"
    assert second.tasks[0].description == "second question"


# --- entrypoint contract --------------------------------------------------------------------
# The response is a BUFFERED dict, not the strands template's SSE stream: `crew.kickoff()` returns
# only when the crew has finished, so there are no incremental tokens to stream. The platform's
# invoke path reads `result`, so these tests pin the key names, not just the happy path.


class _StubCrewOutput:
    """Stands in for CrewAI's `CrewOutput`: text on `.raw`, and NOT JSON-serialisable itself."""

    def __init__(self, raw):
        self.raw = raw


def test_handler_returns_result_from_crew_output(monkeypatch):
    class _StubCrew:
        tasks = ()  # no real Task, so no tool-call bucket

        def kickoff(self):
            return _StubCrewOutput("2 + 2 is 4")

    monkeypatch.setattr(main_module, "build_crew", lambda _prompt: _StubCrew())

    assert handler({"prompt": "what is 2 + 2?"}) == {"result": "2 + 2 is 4", "tool_calls": []}


def test_handler_passes_the_prompt_through(monkeypatch):
    seen = {}

    class _StubCrew:
        tasks = ()  # no real Task, so no tool-call bucket

        def kickoff(self):
            return _StubCrewOutput("ok")

    def _capture(prompt):
        seen["prompt"] = prompt
        return _StubCrew()

    monkeypatch.setattr(main_module, "build_crew", _capture)
    handler({"prompt": "hello there"})

    assert seen["prompt"] == "hello there"


def test_handler_requires_prompt():
    assert handler({}) == {"error": "'prompt' field is required"}
    assert handler({"prompt": ""}) == {"error": "'prompt' field is required"}


def test_handler_reports_crew_errors_instead_of_raising(monkeypatch):
    """A framework/Bedrock fault must reach the caller as a readable error.

    Letting it escape turns a throttle into an opaque 500 with nothing in the body, which is
    indistinguishable from the runtime being down.
    """

    class _BoomCrew:
        tasks = ()  # no real Task, so no tool-call bucket

        def kickoff(self):
            raise RuntimeError("ThrottlingException: rate exceeded")

    monkeypatch.setattr(main_module, "build_crew", lambda _prompt: _BoomCrew())

    assert handler({"prompt": "hi"}) == {"error": "ThrottlingException: rate exceeded"}


def test_handler_response_is_json_serialisable(monkeypatch):
    """`kickoff()` returns a `CrewOutput`, which json cannot encode — only `.raw` may be returned.

    Returning the object whole would fail at the serialiser AFTER the model spend, so the failure
    costs money and produces no answer.
    """
    import json

    class _StubCrew:
        tasks = ()  # no real Task, so no tool-call bucket

        def kickoff(self):
            return _StubCrewOutput("fine")

    monkeypatch.setattr(main_module, "build_crew", lambda _prompt: _StubCrew())

    json.dumps(handler({"prompt": "hi"}))  # raises TypeError if a CrewOutput slipped through
    json.dumps(handler({}))


def test_flush_targets_our_provider_not_the_global_one(monkeypatch):
    """When the global provider is SOMEONE ELSE'S, the flush must still hit ours.

    `set_tracer_provider` is set-once, so if ADOT or a wrapper claimed the global first, our call
    was a silent no-op. The spans still went to our provider (it is passed to `instrument()`
    explicitly), so flushing the global would flush a provider holding none of our spans and let
    the buffered Langfuse spans die with the microVM — silently, and only in production.
    """
    from opentelemetry import trace

    flushed = []

    class _Provider:
        def __init__(self, name):
            self.name = name

        def force_flush(self):
            flushed.append(self.name)

    class _StubCrew:
        tasks = ()  # no real Task, so no tool-call bucket

        def kickoff(self):
            return _StubCrewOutput("answered")

    monkeypatch.setattr(main_module, "build_crew", lambda _prompt: _StubCrew())
    monkeypatch.setattr(main_module, "_tracer_provider", _Provider("ours"))
    monkeypatch.setattr(trace, "get_tracer_provider", lambda: _Provider("someone-else"))

    handler({"prompt": "hi"})

    assert flushed == ["ours"], f"flushed the wrong provider: {flushed}"


def test_handler_does_not_warn_when_telemetry_is_unwired(monkeypatch, caplog):
    """`_tracer_provider is None` is the UNCONFIGURED path — the common local/dev case.

    Nothing to flush and nothing to warn about. An earlier implementation fell back to
    `get_tracer_provider().force_flush()` here, which reaches the no-op `ProxyTracerProvider` —
    it has no `force_flush` attribute at all, so every invocation raised AttributeError inside the
    try and logged "buffered spans may be lost": an alarming warning, on every single turn, about
    spans that were never produced.

    Two things this test must NOT do, both learned the hard way:

    1. **Do not stub the global with an object that has `force_flush`.** The version that did hid
       the defect structurally — the stub could not reproduce the one property that mattered.
    2. **Do not rely on the ambient global provider.** `set_tracer_provider` is set-once and
       PROCESS-WIDE, so `test_telemetry_wires_the_exporter_when_fully_configured` installs a real
       SDK provider that outlives its own test. Running after it, this test saw a provider that
       *does* have `force_flush` and passed against the broken code — it failed in isolation and
       passed in the suite, which is worse than not existing. The real `ProxyTracerProvider` is
       therefore installed explicitly below, so the assertion holds at any position in the suite.
    """
    from opentelemetry.trace import ProxyTracerProvider

    class _StubCrew:
        tasks = ()  # no real Task, so no tool-call bucket

        def kickoff(self):
            return _StubCrewOutput("answered")

    monkeypatch.setattr(main_module, "build_crew", lambda _prompt: _StubCrew())
    monkeypatch.setattr(main_module, "_tracer_provider", None)
    # The unwired global, for real — no `force_flush` attribute at all.
    monkeypatch.setattr(trace_api, "get_tracer_provider", ProxyTracerProvider)

    with caplog.at_level("WARNING"):
        assert handler({"prompt": "hi"}) == {"result": "answered", "tool_calls": []}

    assert "buffered spans may be lost" not in caplog.text


def test_handler_survives_a_failing_span_flush(monkeypatch):
    """The `finally:` flush is best-effort — it must never turn a good answer into a failure.

    The flush exists because the AgentCore microVM freezes between invocations and would drop
    whatever the BatchSpanProcessor had buffered. But losing spans is strictly cheaper than losing
    the turn, so a flush fault is logged and swallowed.

    Faulted on OUR provider, which is the branch a wired agent actually takes — a fault on the
    fallback global would leave the primary path unguarded.
    """

    class _StubCrew:
        tasks = ()  # no real Task, so no tool-call bucket

        def kickoff(self):
            return _StubCrewOutput("still answered")

    def _boom():
        raise RuntimeError("exporter connection refused")

    class _BoomProvider(TracerProvider):  # real, since the handler opens its root span on it
        force_flush = staticmethod(_boom)

    monkeypatch.setattr(main_module, "build_crew", lambda _prompt: _StubCrew())
    monkeypatch.setattr(main_module, "_tracer_provider", _BoomProvider())

    assert handler({"prompt": "hi"}) == {"result": "still answered", "tool_calls": []}


# --- trace user id --------------------------------------------------------------------------
# Langfuse's Traces USER column reads `langfuse.user.id`. The handler opens a root span carrying
# the caller's Entra oid, and the CrewAI instrumentation's spans nest under it — otherwise every
# trace from a template agent shows USER "—".


def _bearer(claims: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"Bearer e30.{body}.sig"


class _SpanCheckingCrew:
    tasks = ()  # no real Task, so no tool-call bucket
    ran_inside_span = False

    def kickoff(self):
        _SpanCheckingCrew.ran_inside_span = trace_api.get_current_span().get_span_context().is_valid
        return _StubCrewOutput("ok")


def _traced(monkeypatch, headers=None):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(main_module, "_tracer_provider", provider)
    monkeypatch.setattr(
        main_module.BedrockAgentCoreContext, "get_request_headers", classmethod(lambda cls: headers)
    )
    monkeypatch.setattr(_SpanCheckingCrew, "ran_inside_span", False)
    monkeypatch.setattr(main_module, "build_crew", lambda _prompt: _SpanCheckingCrew())
    return exporter


def _user_ids(exporter):
    return [s.attributes.get("langfuse.user.id") for s in exporter.get_finished_spans()]


def test_trace_user_id_from_payload_user_oid(monkeypatch):
    """The platform forwards the authenticated caller's oid as `user_oid`."""
    exporter = _traced(monkeypatch)
    assert handler({"prompt": "hi", "user_oid": "oid-from-platform"}) == {"result": "ok", "tool_calls": []}
    assert _user_ids(exporter) == ["oid-from-platform"]
    assert _SpanCheckingCrew.ran_inside_span, "the crew's spans must nest under the user-tagged root"


def test_trace_user_id_prefers_the_inbound_bearer_oid(monkeypatch):
    """The inbound bearer was verified by the runtime's JWT authorizer, so its oid wins."""
    exporter = _traced(monkeypatch, headers={"Authorization": _bearer({"oid": "oid-from-token"})})
    handler({"prompt": "hi", "user_oid": "oid-from-platform"})
    assert _user_ids(exporter) == ["oid-from-token"]


def test_no_caller_oid_still_opens_the_root_span_without_a_user(monkeypatch):
    """No oid anywhere (a local curl): same root span name, so traces stay consistent; no user."""
    exporter = _traced(monkeypatch, headers={"Authorization": "Bearer not-a-jwt"})
    assert handler({"prompt": "hi"}) == {"result": "ok", "tool_calls": []}
    assert [s.name for s in exporter.get_finished_spans()] == ["invoke_agent"]
    assert _user_ids(exporter) == [None]


def test_invalid_user_oid_is_ignored(monkeypatch):
    """A direct caller controls the payload: a non-string or oversized oid is dropped, not sent."""
    exporter = _traced(
        monkeypatch,
    )
    handler({"prompt": "hi", "user_oid": {"not": "a string"}})
    handler({"prompt": "hi", "user_oid": "x" * 129})
    assert _user_ids(exporter) == [None, None]


# --- evaluations: tool calls + CloudWatch traces (E42) --------------------------------------
# The tool checks read `tool_calls` off the response, collected from CrewAI's real event bus; the
# judges read the spans the second (X-Ray OTLP) processor exports. tests/conftest.py switches that
# export off at import, so these drive `_setup_cloudwatch_traces()` directly.


def test_handler_returns_this_invokes_tool_calls_from_the_event_bus(monkeypatch):
    from types import SimpleNamespace

    from crewai.events import ToolUsageStartedEvent, crewai_event_bus

    def _started(task_id, name, args):
        crewai_event_bus.emit(
            object(), ToolUsageStartedEvent(tool_name=name, tool_args=args, task_id=task_id)
        )

    class _ToolCrew:
        tasks = (SimpleNamespace(id="task-1"),)

        def kickoff(self):
            _started("task-1", "get_policy", {"policy_id": "P-1"})  # native path: a dict
            _started("other-invoke", "not_mine", {})  # another invoke's task: ignored
            _started("task-1", "open_claim", '{"kind": "auto"}')  # ReAct path: JSON text
            crewai_event_bus.flush()  # what a real Crew does before kickoff() returns
            return _StubCrewOutput("claim opened")

    monkeypatch.setattr(main_module, "build_crew", lambda _prompt: _ToolCrew())

    assert handler({"prompt": "hi"}) == {
        "result": "claim opened",
        "tool_calls": [
            {"name": "get_policy", "args": {"policy_id": "P-1"}},
            {"name": "open_claim", "args": {"kind": "auto"}},
        ],
    }
    assert main_module._tool_calls_by_task == {}  # the bucket is closed after the invoke


def test_tool_calls_without_a_name_are_skipped(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setitem(main_module._tool_calls_by_task, "task-1", [])
    for name in (None, "get_policy"):
        event = SimpleNamespace(task_id="task-1", tool_name=name, tool_args={}, emission_sequence=1)
        main_module._record_tool_call(object(), event)

    assert main_module._tool_calls_by_task["task-1"] == [(1, {"name": "get_policy", "args": {}})]


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
