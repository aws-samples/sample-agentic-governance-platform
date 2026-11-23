"""CrewAI Agent Template — Bedrock AgentCore Entry Point.

A barebones single-crew CrewAI agent: one agent, one task, one crew.
Deploys to Bedrock AgentCore via BedrockAgentCoreApp.

Local: python -m src.main (starts on http://localhost:8080)
Docker: docker build -t my-agent . && docker run -p 8080:8080 my-agent
Test: curl -X POST http://localhost:8080/invocations -d '{"prompt": "hello"}'
"""

import base64
import contextlib
import json
import logging
import os

import boto3

from src.config import config

logging.basicConfig(level=config.LOG_LEVEL, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# SET FIRST — before ANY code path that can import `crewai`, which includes the Langfuse block
# below (its instrumentor imports crewai to patch it) as well as the explicit import further down.
# CrewAI ships an opt-OUT telemetry client that phones home to CrewAI Inc's own servers, and it
# reads this flag when `crewai.telemetry` is imported. Governance-relevant, not just noise: an
# agent running in a customer's account must not emit usage data to a third party nobody in the
# platform's trust chain reviewed. `setdefault`, so an operator can still turn it back on.
#
# THE ORDERING IS THE WHOLE POINT, and getting it wrong is invisible from the env var. This line
# once sat BELOW the Langfuse block, which meant the instrumentor imported crewai while the flag
# was still unset: the variable then read "true" while crewai's telemetry was already live
# (`Telemetry().ready is True`), its signal handlers were installed, and its span processor had
# attached itself to the platform's OWN Langfuse provider. That fired only on the
# Langfuse-configured path — i.e. exclusively in production. `tests/test_agent.py` asserts
# `Telemetry().ready is False`, because asserting the env string cannot catch this.
#
# This is DISTINCT from the Langfuse block below and neither replaces the other: that one ships
# traces to the platform's OWN Langfuse project; this one stops an unrelated export to a vendor.
os.environ.setdefault("CREWAI_DISABLE_TELEMETRY", "true")


# --- Langfuse observability -----------------------------------------------------------------
# EVERY agent provisioned from this template is observable with zero manual wiring: the platform
# provisions one Langfuse project + key PER AGENT and injects the NON-SECRET host + secret NAME
# into the runtime env (LANGFUSE_HOST / LANGFUSE_SECRET_NAME). This block reads the per-agent
# {public_key, secret_key} pair from AWS Secrets Manager and points the OTLP exporter at that
# project — attribution is STRUCTURAL (each agent authenticates with its own project key, so
# traces land in its own project). The project key is NEVER a hardcoded literal; the secret VALUE
# lives ONLY in Secrets Manager and is never logged (the base64 auth IS the secret).
#
# Runs BEFORE the crewai import below, which is why those imports are not at the top of this
# module: the instrumentor patches CrewAI's classes, and the OTEL env has to be in place before
# the exporter it configures is constructed. Note it runs AFTER the vendor-telemetry flag above,
# and that order is not interchangeable — the instrumentor imports crewai itself, so the flag must
# already be set when this block runs. Graceful degrade throughout: if the env/secret is absent or
# unreadable, telemetry is simply not wired and the agent still runs.


# OUR TracerProvider, once telemetry is wired — the one the entrypoint's flush must target. Stays
# None on every degraded path, which is also the signal that there is nothing of ours to flush.
_tracer_provider = None


def _setup_langfuse_telemetry() -> None:
    host = config.LANGFUSE_HOST.rstrip("/")
    secret_name = config.LANGFUSE_SECRET_NAME
    if not host or not secret_name:
        logger.info("Langfuse telemetry disabled: LANGFUSE_HOST / LANGFUSE_SECRET_NAME not set.")
        return

    try:
        sm = boto3.client("secretsmanager", region_name=config.AWS_REGION)
        secret = json.loads(sm.get_secret_value(SecretId=secret_name)["SecretString"])
        public_key = secret.get("public_key", "")
        secret_key = secret.get("secret_key", "")
    except Exception as exc:
        logger.warning(
            "Langfuse telemetry disabled: could not read secret %s (%r)", secret_name, exc, exc_info=True
        )
        return

    if not public_key or not secret_key:
        logger.warning("Langfuse telemetry disabled: secret %s missing public_key/secret_key.", secret_name)
        return

    # Basic auth base64("{public}:{secret}"). NEVER log this value / the Authorization header.
    lf_auth = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()

    # AgentCore auto-instruments with ADOT and owns the global OTEL TracerProvider first
    # (set-once) — disabling ADOT lets the Langfuse exporter win. Set only once Langfuse is configured.
    os.environ["DISABLE_ADOT_OBSERVABILITY"] = "true"
    # BASE endpoint form: the HTTP exporter auto-appends /v1/traces — do NOT add it here.
    os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = f"{host}/api/public/otel"
    os.environ["OTEL_EXPORTER_OTLP_HEADERS"] = f"Authorization=Basic {lf_auth}"
    # Langfuse supports http/protobuf + http/json; gRPC is NOT supported.
    os.environ["OTEL_EXPORTER_OTLP_PROTOCOL"] = "http/protobuf"
    os.environ.setdefault("OTEL_SERVICE_NAME", os.environ.get("AGENT_NAME", "crewai-agentcore-agent"))

    # WRAPPED, because this tail runs at MODULE IMPORT and an exception here does not degrade
    # telemetry — it kills the agent before `app.run()` and the runtime crash-loops. An agent with
    # no traces is degraded; an agent that will not start is down. Logged WITHOUT the auth
    # header/keys: `exc` is the import/exporter fault, and the secret only ever reaches os.environ
    # above — a traceback is not a place for a credential.
    #
    # Unlike Strands (whose `[otel]` extra wires a provider for us), nothing here has installed a
    # real TracerProvider: AgentCore's ADOT was just disabled above, so the global provider is
    # still the no-op default and spans would be dropped silently. Build one explicitly, pointed
    # at the env configured above, THEN instrument.
    global _tracer_provider
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.crewai import CrewAIInstrumentor
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider = TracerProvider()
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        # `set_tracer_provider` is SET-ONCE: if anything claimed the global first, this call is a
        # silent no-op (a log line from OTEL, nothing raised). The spans still reach OUR provider,
        # because it is passed to `instrument()` explicitly below rather than looked up globally —
        # so the module-level reference kept here is what the flush must target. Flushing
        # `get_tracer_provider()` instead would flush SOMEONE ELSE'S provider and let our buffered
        # Langfuse spans die with the microVM, silently and only in production.
        trace.set_tracer_provider(provider)
        _tracer_provider = provider
        CrewAIInstrumentor().instrument(tracer_provider=provider)
        logger.info("Langfuse telemetry enabled for host %s", host)
    except Exception as exc:
        logger.warning("Langfuse telemetry disabled: crewai instrumentation failed (%r)", exc, exc_info=True)


_setup_langfuse_telemetry()
# --- end Langfuse observability -------------------------------------------------------------


# --- CloudWatch traces (E42) ----------------------------------------------------------------
# A SECOND span processor on the same provider sends every span to CloudWatch as well, because
# AgentCore Evaluations judges this agent from its CloudWatch spans (README → "Evaluations"). This
# is NOT ADOT auto-instrumentation: it is one explicit OTLP exporter pointed at the X-Ray OTLP
# endpoint, signed with SigV4 from the runtime role. The signing is the same mechanism as
# aws-opentelemetry-distro's `OTLPAwsSpanExporter`
# (amazon/opentelemetry/distro/exporter/otlp/aws/traces/otlp_aws_span_exporter.py, whose
# AwsAuthSession in .../aws/common/aws_auth_session.py is a requests.Session that SigV4-signs each
# POST for service "xray"), written out here because the distro pins the OTEL SDK to one exact
# version plus ~50 instrumentations, which would fight the ranges this template pins.
# On by default; AGP_CLOUDWATCH_TRACES=false turns it off. Any setup fault is logged and the agent
# runs without it, the same one-sided rule as the Langfuse block. Runs AFTER the vendor-telemetry
# flag at the top, for the same reason that block does (the instrumentor imports crewai).


_XRAY_SENT_HEADERS = {"content-type", "content-encoding", "user-agent"}  # + SigV4's own


def _xray_span_exporter(region: str):
    """An OTLP/HTTP span exporter for ``https://xray.{region}.amazonaws.com/v1/traces``, SigV4-signed."""
    import botocore.session
    import requests
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from requests.structures import CaseInsensitiveDict

    credentials = botocore.session.get_session().get_credentials()
    if credentials is None:
        raise RuntimeError("no AWS credentials to sign X-Ray span exports with")

    class _SigV4Session(requests.Session):
        def request(self, method, url, *args, data=None, headers=None, **kwargs):
            # The exporter also picks up OTEL_EXPORTER_OTLP_HEADERS, which the Langfuse block sets
            # to its Basic auth. So AWS gets an ALLOWLIST: the payload headers plus what SigV4 adds.
            # Every other header, on the session or on the call, is dropped (None removes a session
            # header from requests' merge).
            given = CaseInsensitiveDict({**self.headers, **(headers or {})})
            kept = {k: v for k, v in given.items() if k.lower() in _XRAY_SENT_HEADERS}
            signed = AWSRequest(method=method, url=url, data=data, headers=kept)
            # Frozen once per request, so a refresh can't land between the key, secret and token reads.
            SigV4Auth(credentials.get_frozen_credentials(), "xray", region).add_auth(signed)
            sent = CaseInsensitiveDict({k: None for k in self.headers})
            sent.update(signed.headers.items())
            return super().request(method, url, *args, data=data, headers=sent, **kwargs)

    endpoint = f"https://xray.{region}.amazonaws.com/v1/traces"
    return OTLPSpanExporter(endpoint=endpoint, session=_SigV4Session())


def _setup_cloudwatch_traces() -> None:
    if os.environ.get("AGP_CLOUDWATCH_TRACES", "").strip().lower() == "false":
        logger.info("CloudWatch traces disabled: AGP_CLOUDWATCH_TRACES=false.")
        return

    global _tracer_provider
    try:
        from bedrock_agentcore.runtime.context import BedrockAgentCoreContext
        from opentelemetry.sdk.trace import SpanProcessor
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        class _SessionIdProcessor(SpanProcessor):
            """Tags each span with the AgentCore session id, which is how CloudWatch spans are
            found per invoke. The runtime sets it per request from its session-id header."""

            def on_start(self, span, parent_context=None):
                session_id = BedrockAgentCoreContext.get_session_id()
                if session_id:
                    span.set_attribute("session.id", session_id)

        exporter = _xray_span_exporter(config.AWS_REGION)
        if _tracer_provider is None:
            # Langfuse is not wired, so nothing has built a provider yet. Build and instrument one
            # exactly as the Langfuse block does (see there for why it must be explicit).
            from opentelemetry import trace
            from opentelemetry.instrumentation.crewai import CrewAIInstrumentor
            from opentelemetry.sdk.trace import TracerProvider

            os.environ["DISABLE_ADOT_OBSERVABILITY"] = "true"
            provider = TracerProvider()
            trace.set_tracer_provider(provider)
            _tracer_provider = provider
            CrewAIInstrumentor().instrument(tracer_provider=provider)
        _tracer_provider.add_span_processor(_SessionIdProcessor())
        _tracer_provider.add_span_processor(BatchSpanProcessor(exporter))
        logger.info("CloudWatch traces enabled (X-Ray OTLP endpoint, %s)", config.AWS_REGION)
    except Exception as exc:
        logger.warning(
            "CloudWatch traces disabled: X-Ray span exporter setup failed (%r)", exc, exc_info=True
        )


_setup_cloudwatch_traces()
# --- end CloudWatch traces ------------------------------------------------------------------

# Imported HERE, not at module top, for both reasons above: the vendor-telemetry flag and the OTEL
# env must both be in place first. E402 (the rule that forbids exactly this ordering) is therefore
# ignored for this file in pyproject.toml, with the reason recorded there; do NOT "fix" the ordering
# to satisfy it. Keep them grouped so isort leaves them put.
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from bedrock_agentcore.runtime.context import BedrockAgentCoreContext
from crewai import LLM, Agent, Crew, Task
from crewai.events import ToolUsageStartedEvent, crewai_event_bus

# --- Tool calls (E42) ----------------------------------------------------------------------
# Evaluations check which tools an invoke called. CrewAI emits `ToolUsageStartedEvent` on its
# event bus for both of its tool paths (ReAct and native function calling; `step_callback` misses
# the native one), and the bus is process-wide, so the calls are bucketed by the task id each
# event carries: an invoke opens a bucket for its own tasks and reads it back after `kickoff()`,
# which flushes the bus before it returns. Handlers run on the bus's thread pool, so they can land
# out of order; each call is kept with the event's `emission_sequence` (stamped synchronously at
# emit) and sorted on read.
_tool_calls_by_task: dict[str, list[tuple[int, dict]]] = {}


@crewai_event_bus.on(ToolUsageStartedEvent)
def _record_tool_call(_source, event) -> None:
    calls = _tool_calls_by_task.get(event.task_id or "")
    if calls is None or not isinstance(event.tool_name, str):  # no name: the backend's ToolCall needs one
        return
    args = event.tool_args
    if isinstance(args, str):  # the ReAct path hands over the raw JSON text
        try:
            args = json.loads(args) if args.strip() else {}
        except ValueError:
            args = {}
    call = {"name": event.tool_name, "args": args if isinstance(args, dict) else {}}
    calls.append((event.emission_sequence or 0, call))


# --- Crew definition -----------------------------------------------------------------------
#
# Edit these four strings and you have a different agent. CrewAI's prompt is assembled from the
# ROLE / GOAL / BACKSTORY triple (they are not decoration — they are the system prompt), plus the
# per-task `description` and `expected_output`.
#
# Kept to ONE agent and ONE task on purpose: this is the scaffold a customer grows, and a
# multi-agent crew is a design decision they should make, not inherit. To add a second agent,
# append it to `agents=[...]` and give it its own `Task`; CrewAI runs tasks sequentially by
# default, passing each task's output forward as context.

AGENT_ROLE = "Helpful Assistant"
AGENT_GOAL = "Answer the user's question accurately and concisely."
AGENT_BACKSTORY = (
    "You are a seasoned generalist assistant. You answer plainly, you do not pad your answers, "
    "and when you are not certain of something you say so instead of guessing."
)
EXPECTED_OUTPUT = "A direct, concise answer to the user's question in plain prose."


def build_crew(prompt: str) -> Crew:
    """Assemble the crew for ONE invocation.

    Built per-call rather than at module import, which is the opposite of the strands template's
    module-level `Agent`, and deliberate: a CrewAI `Task` CARRIES the request (`description=prompt`)
    and a `Crew` carries per-run execution state. A module-level crew would either have to be
    mutated on every request — a data race the moment two invocations overlap — or reused with the
    first request's task, which is worse than slow: it is wrong, silently.

    The `LLM` is constructed here too. It is cheap (a LiteLLM config object; no network, no
    credential resolution at construction) and keeping it inside the function means a MODEL_ID
    change needs no import-time reasoning about what was captured when.
    """
    llm = LLM(model=config.CREWAI_MODEL, temperature=0.3, max_tokens=4096)
    agent = Agent(
        role=AGENT_ROLE,
        goal=AGENT_GOAL,
        backstory=AGENT_BACKSTORY,
        llm=llm,
        # No `tools=[...]` in the starter — add them here (see README → "Adding tools").
        verbose=False,
        # OFF deliberately. CrewAI's delegation lets an agent hand work to another agent in the
        # crew; with a single agent there is nobody to delegate to, and leaving it on spends tokens
        # on a tool call that can only fail.
        allow_delegation=False,
    )
    task = Task(description=prompt, expected_output=EXPECTED_OUTPUT, agent=agent)
    return Crew(agents=[agent], tasks=[task], verbose=False)


_MAX_OID_LEN = 128  # an Entra oid is a 36-char GUID; generous headroom


def _caller_oid(payload: dict) -> str | None:
    """The calling user's Entra ``oid``, for the Langfuse Traces USER column. ``None`` if unknown.

    Prefers the inbound bearer, which the runtime's JWT authorizer has already verified (read here
    without re-verifying; only the ``oid`` claim leaves this function — never the token). Falls back
    to ``user_oid`` in the payload, which the platform sets from the caller it authenticated: the
    inbound Authorization header usually does not reach the container. Telemetry only, so this
    never raises.
    """
    token_oid = None
    try:
        headers = BedrockAgentCoreContext.get_request_headers() or {}
        auth = headers.get("Authorization") or headers.get("authorization") or ""
        if auth.startswith("Bearer "):
            seg = auth[len("Bearer ") :].split(".")[1]
            token_oid = json.loads(base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4))).get("oid")
    except Exception as exc:
        logger.debug(
            "Caller oid not readable from the inbound bearer (%s)", type(exc).__name__, exc_info=True
        )
    for oid in (token_oid, payload.get("user_oid")):
        # A direct caller can put anything in the payload: a non-string or an implausibly long
        # value is ignored (not truncated), since a mangled id would mislabel the trace.
        if isinstance(oid, str) and 0 < len(oid) <= _MAX_OID_LEN:
            return oid
    return None


def _user_span(payload: dict):
    """The root ``invoke_agent`` span, tagged with the caller when there is one; no-op if unwired.

    Opened for every invoke, oid or not, so every trace from this agent has the same root name.

    The CrewAI instrumentation's spans nest under the current span, so one attribute here reaches
    the whole trace. Key is the Langfuse OTEL mapping spelling (`langfuse.user.id`).
    """
    if _tracer_provider is None:
        return contextlib.nullcontext()
    oid = _caller_oid(payload)
    return _tracer_provider.get_tracer(__name__).start_as_current_span(
        "invoke_agent", attributes={"langfuse.user.id": oid} if oid else None
    )


# --- AgentCore App -------------------------------------------------------------------------

app = BedrockAgentCoreApp()


@app.entrypoint
def handler(payload: dict, context=None) -> dict:
    """Handle agent invocations.

    Payload: ``{"prompt": "user message"}``

    Response: ``{"result": "<answer>", "tool_calls": [{"name": ..., "args": {...}}]}`` — a single
    JSON object, not a stream (``tool_calls`` is for evaluations, README → "Evaluations"). Errors
    answer ``{"error": "<message>"}`` with the same shape rule: one key, a string value.

    **This is a BUFFERED contract, and it differs from the strands template's on purpose.**
    `crew.kickoff()` is synchronous and returns only when the whole crew has finished, so there
    are no incremental tokens to stream — an SSE wrapper here would emit one chunk and add a
    second contract for a client to get wrong. AgentCore serialises this dict as the response body.

    A buffering client (the platform's ``POST /agents/{id}/invoke``) reads ``result`` directly.
    Keep the key name: it is what the platform's response contract for config/buffered templates
    keys on, and renaming it is a breaking change for every agent materialized from this template.

    Note ``result.raw`` below, not ``result``: ``kickoff()`` returns a ``CrewOutput`` object
    (raw text plus token usage and per-task outputs), which is NOT JSON-serialisable. Returning it
    whole would fail at the serialiser, after the model spend.
    """
    prompt = payload.get("prompt", "")
    if not prompt:
        return {"error": "'prompt' field is required"}

    logger.info("Invocation received: %s...", prompt[:50])
    task_ids: list[str] = []
    bucket: list[tuple[int, dict]] = []
    try:
        crew = build_crew(prompt)
        # One shared list for every task of this crew, so a multi-task crew's calls stay in order.
        task_ids = [str(task.id) for task in crew.tasks]
        for task_id in task_ids:
            _tool_calls_by_task[task_id] = bucket
        with _user_span(payload):
            result = crew.kickoff()
    except Exception as exc:
        # Blind by intent: whatever the framework or Bedrock raises (throttling, validation, a
        # LiteLLM provider fault), the runtime must answer with the error rather than let the
        # exception escape and become an opaque 500 the caller cannot read.
        logger.exception("Crew execution failed")
        return {"error": str(exc)}
    finally:
        # Close this invoke's tool-call buckets on every path, so the dict never grows.
        for task_id in task_ids:
            _tool_calls_by_task.pop(task_id, None)
        # Force-flush OTEL spans before the AgentCore microVM freezes: the background
        # BatchSpanProcessor would otherwise lose buffered Langfuse spans. Best-effort.
        #
        # Flushes THE PROVIDER THIS MODULE BUILT, not `get_tracer_provider()`. The global is a
        # `ProxyTracerProvider` whenever telemetry is unwired — and it has no `force_flush`
        # attribute at all, so calling it raised AttributeError on EVERY invocation and logged a
        # scary warning about lost spans on the path where there were simply no spans to lose. It
        # can also legitimately be someone else's provider, since `set_tracer_provider` is
        # set-once. `None` means telemetry was never wired, so there is nothing to flush and
        # nothing to warn about — hence the guard sits OUTSIDE the try, with no fallback branch.
        if _tracer_provider is not None:
            try:
                _tracer_provider.force_flush()
            except Exception:
                # LOGGED, not swallowed — losing spans is invisible otherwise, and "traces are
                # empty" would be indistinguishable from "the agent was never invoked". A flush
                # fault must never fail the turn.
                logger.warning("OTEL span flush failed; buffered spans may be lost", exc_info=True)

    return {"result": result.raw, "tool_calls": [call for _, call in sorted(bucket, key=lambda c: c[0])]}


if __name__ == "__main__":
    app.run()
